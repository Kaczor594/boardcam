"""How likely a player of a given strength is to play each move.

Three pieces, combined by :class:`Mover`:

* :class:`Maia` — the Maia-1 policy network (CSSLab, v1.0 weights in
  ``data/maia/``) run through ``lc0`` at one node, with the policy read from
  ``VerboseMoveStats`` at softmax temperature 1 (lc0's default 1.359 flattens
  it). One lc0 process per band, started lazily, cached per position key.
* :func:`band_for` — chess.com rating → Maia band (Lichess scale), from an
  offset fitted on real moves (``python -m engine.humanlike fit``; the result
  is ``engine/humanlike.json``).
* :class:`OpeningPrior` — what a player actually played from positions they
  have reached before, blended with Maia as a Dirichlet prior:
  ``p(m) = (count(m) + k·maia(m)) / (N + k)``. ``k`` is fitted too.

Positions are passed to lc0 as a FEN without history (lc0's ``fen_only``
history fill). That keeps the cache key a position, at a small cost in policy
quality against Maia's training setup.

The chess-insights SQLite database (``CHESS_INSIGHTS_DB``, default
``~/claude-projects/chess-insights/data/chess_insights.db``) is the only source
of real games. :func:`load_games` parses it once and caches the move lists
under ``data/cache/``.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import pickle
import random
import re
import sqlite3
import subprocess
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import chess
import chess.pgn

ROOT = Path(__file__).resolve().parent.parent
MAIA_DIR = ROOT / "data" / "maia"
CACHE_DIR = ROOT / "data" / "cache"
CONFIG = Path(__file__).with_name("humanlike.json")
BANDS = tuple(range(1100, 2000, 100))
DEFAULT_DB = Path.home() / "claude-projects/chess-insights/data/chess_insights.db"
P_FLOOR = 5e-5          # lc0 prints P to 0.01 %; a printed 0.00 % becomes this

_P_LINE = re.compile(r"^info string (\S+)\s+\(.*?\(P:\s*([\d.]+)%\)")


def position_key(board: chess.Board) -> str:
    """Placement, side to move, castling, en passant: what the policy depends on."""
    return " ".join(board.fen(en_passant="legal").split()[:4])


def fast_key(board: chess.Board) -> tuple:
    """The same identity as :func:`position_key` as bitboards (python-chess's
    transposition key), for hot loops."""
    return board._transposition_key()


# --------------------------------------------------------------------------- Maia

class Maia:
    """lc0 + one Maia network per band, policy only (one node).

    ``workers`` lc0 processes per band; :meth:`policy_many` spreads a batch of
    cache misses over them on threads (the GIL is released while waiting on lc0).
    """

    def __init__(self, maia_dir: Path = MAIA_DIR, lc0: str = "lc0", workers: int = 1,
                 backend: str = "blas"):
        self.maia_dir = Path(maia_dir)
        self.backend = backend      # CPU BLAS: 0.6 ms/eval vs 2.0 ms on metal for this small net
        self.lc0 = lc0
        self.workers = max(1, workers)
        self._procs: dict[tuple[int, int], subprocess.Popen] = {}
        self._locks: defaultdict[tuple[int, int], threading.Lock] = defaultdict(threading.Lock)
        self._cache: dict[tuple[int, tuple], dict[str, float]] = {}
        self._pool = None
        self.evals = 0          # network calls actually made (cache misses)

    def _proc(self, band: int, slot: int) -> subprocess.Popen:
        p = self._procs.get((band, slot))
        if p is not None and p.poll() is None:
            return p
        weights = self.maia_dir / f"maia-{band}.pb.gz"
        if not weights.exists():
            raise FileNotFoundError(f"no Maia weights for band {band}: {weights}")
        p = subprocess.Popen(
            [self.lc0, f"--weights={weights}", "--threads=1", "--minibatch-size=1",
             "--max-prefetch=0", "--verbose-move-stats", "--policy-softmax-temp=1.0",
             f"--backend={self.backend}"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, bufsize=1)
        self._procs[(band, slot)] = p
        self._send(p, "uci")
        self._read_until(p, "uciok")
        self._send(p, "isready")
        self._read_until(p, "readyok")
        return p

    @staticmethod
    def _send(p, line: str) -> None:
        p.stdin.write(line + "\n")
        p.stdin.flush()

    @staticmethod
    def _read_until(p, token: str) -> list[str]:
        out = []
        while True:
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("lc0 exited")
            out.append(line)
            if line.startswith(token):
                return out

    def _eval(self, board: chess.Board, band: int, slot: int = 0) -> dict[str, float]:
        legal = [m.uci() for m in board.legal_moves]
        if not legal:
            return {}
        with self._locks[(band, slot)]:
            p = self._proc(band, slot)
            self._send(p, "position fen " + board.fen())
            self._send(p, "go nodes 1")
            lines = self._read_until(p, "bestmove")
        self.evals += 1
        raw: dict[str, float] = {}
        for line in lines:
            m = _P_LINE.match(line)
            if m and m.group(1) != "node":
                raw[m.group(1)] = float(m.group(2)) / 100.0   # lc0 prints e1g1, a7a8q
        pol = {u: max(raw.get(u, 0.0), P_FLOOR) for u in legal}
        z = sum(pol.values())
        return {u: v / z for u, v in pol.items()}

    def policy(self, board: chess.Board, band: int) -> dict[str, float]:
        """Move (UCI) → probability over the legal moves; empty when there are none."""
        key = (band, fast_key(board))
        hit = self._cache.get(key)
        if hit is None:
            hit = self._cache[key] = self._eval(board, band)
        return hit

    def policy_many(self, boards: list[chess.Board], band: int) -> None:
        """Fill the cache for ``boards`` using every lc0 process of the band."""
        todo, seen = [], set()
        for b in boards:
            k = (band, fast_key(b))
            if k not in self._cache and k not in seen:
                seen.add(k)
                todo.append((k, b))
        if not todo:
            return
        if self.workers == 1 or len(todo) < 2:
            for k, b in todo:
                self._cache[k] = self._eval(b, band)
            return
        from concurrent.futures import ThreadPoolExecutor
        if self._pool is None:
            self._pool = ThreadPoolExecutor(self.workers)
        chunks = [todo[i::self.workers] for i in range(self.workers)]

        def run(slot):
            return [(k, self._eval(b, band, slot)) for k, b in chunks[slot]]

        for part in self._pool.map(run, range(self.workers)):
            for k, pol in part:
                self._cache[k] = pol

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None
        for p in self._procs.values():
            try:
                self._send(p, "quit")
                p.wait(timeout=2)
            except Exception:
                p.kill()
        self._procs.clear()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


# ----------------------------------------------------------------- rating → band

def load_config() -> dict:
    if CONFIG.exists():
        return json.loads(CONFIG.read_text())
    return {"offset": 0, "k": 5.0}


def band_for(rating_chesscom: float, offset: float | None = None) -> int:
    """The Maia band for a chess.com rating: nearest band to ``rating + offset``."""
    if offset is None:
        offset = load_config()["offset"]
    r = rating_chesscom + offset
    return min(BANDS, key=lambda b: (abs(b - r), b))


# ---------------------------------------------------------------- real games

@dataclass
class Game:
    id: int
    date: str
    platform: str
    white: str
    black: str
    white_rating: int | None
    black_rating: int | None
    base_s: int | None
    moves: list[str] = field(default_factory=list)   # UCI from the start position


def db_path() -> Path:
    return Path(os.environ.get("CHESS_INSIGHTS_DB", DEFAULT_DB))


def load_games(db: Path | None = None) -> list[Game]:
    """Every game in the chess-insights database with its moves, oldest first."""
    db = Path(db or db_path())
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stamp = f"{db.stat().st_size}-{int(db.stat().st_mtime)}"
    cache = CACHE_DIR / "games.pkl"      # plain tuples: independent of how this module runs
    if cache.exists():
        with cache.open("rb") as f:
            saved = pickle.load(f)
        if saved.get("stamp") == stamp:
            return [Game(*t) for t in saved["games"]]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select g.game_id, g.date_played, g.platform, pw.username, pb.username, "
        "g.white_rating, g.black_rating, g.time_control_seconds, g.pgn "
        "from games g join players pw on pw.player_id = g.white_player_id "
        "join players pb on pb.player_id = g.black_player_id "
        "order by g.date_played").fetchall()
    con.close()
    import io
    games = []
    for gid, date, plat, w, b, wr, br, base, pgn in rows:
        g = chess.pgn.read_game(io.StringIO(pgn))
        if g is None or g.errors or g.headers.get("SetUp") == "1" \
                or g.headers.get("Variant", "Standard") not in ("Standard", "Chess"):
            continue
        moves = [m.uci() for m in g.mainline_moves()]
        games.append(Game(gid, date or "", plat, w.lower(), b.lower(), wr, br, base, moves))
    with cache.open("wb") as f:
        pickle.dump({"stamp": stamp, "games": [
            (g.id, g.date, g.platform, g.white, g.black, g.white_rating, g.black_rating,
             g.base_s, g.moves) for g in games]}, f)
    return games


def accounts_file() -> Path:
    return ROOT / "data" / "players.json"


def accounts_for(name: str) -> set[str]:
    """Online usernames of a player name from the game settings (``data/players.json``,
    untracked: ``{"Isaac": ["kaczor594", "simsimlamachine"]}``). Unknown → empty."""
    f = accounts_file()
    if not f.exists() or not name:
        return set()
    table = {k.lower(): v for k, v in json.loads(f.read_text()).items()}
    return {u.lower() for u in table.get(name.strip().lower(), [])}


# ------------------------------------------------------------- opening prior

class OpeningPrior:
    """Counts of the moves a player made from each position key."""

    def __init__(self, counts: dict[str, Counter] | None = None):
        self.counts: dict[str, Counter] = counts or {}

    @classmethod
    def build(cls, games: list[Game], accounts: set[str], *, before: str | None = None,
              since: str | None = None, max_ply: int = 40) -> "OpeningPrior":
        counts: dict[str, Counter] = defaultdict(Counter)
        for g in games:
            if before and g.date >= before:
                continue
            if since and g.date < since:
                continue
            sides = {chess.WHITE} if g.white in accounts else set()
            if g.black in accounts:
                sides.add(chess.BLACK)
            if not sides:
                continue
            board = chess.Board()
            for uci in g.moves[:max_ply]:
                if board.turn in sides:
                    counts[position_key(board)][uci] += 1
                board.push_uci(uci)
        return cls(dict(counts))

    def __len__(self) -> int:
        return len(self.counts)

    def blend(self, board: chess.Board, maia: dict[str, float], k: float) -> dict[str, float]:
        c = self.counts.get(position_key(board))
        if not c:
            return maia
        n = sum(c.values())
        return {u: (c.get(u, 0) + k * p) / (n + k) for u, p in maia.items()}


# ---------------------------------------------------------------------- Mover

@dataclass
class Side:
    band: int
    prior: OpeningPrior | None = None


class Mover:
    """The move distribution at a position, by whose turn it is.

    ``policy(board)`` is what the line solver calls; it counts network calls in
    ``maia.evals`` and serves repeats from the cache.
    """

    def __init__(self, white: Side, black: Side, maia: Maia | None = None, k: float | None = None):
        self.sides = {chess.WHITE: white, chess.BLACK: black}
        self.maia = maia or Maia()
        self.k = load_config()["k"] if k is None else k
        self._cache: dict[tuple[bool, str], dict[str, float]] = {}

    def prefetch(self, boards: list[chess.Board]) -> None:
        """Evaluate a batch of positions in parallel ahead of :meth:`policy`."""
        for color in chess.COLORS:
            group = [b for b in boards if b.turn == color
                     and (color, fast_key(b)) not in self._cache]
            if group:
                self.maia.policy_many(group, self.sides[color].band)

    def policy(self, board: chess.Board) -> dict[str, float]:
        key = (board.turn, fast_key(board))
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        side = self.sides[board.turn]
        pol = self.maia.policy(board, side.band)
        if side.prior is not None:
            pol = side.prior.blend(board, pol, self.k)
        self._cache[key] = pol
        return pol


def for_game(white_rating: float, black_rating: float, *, white_name: str = "",
             black_name: str = "", maia: Maia | None = None,
             games: list[Game] | None = None) -> Mover:
    """A :class:`Mover` from a BoardCam game's settings (chess.com ratings, names)."""
    sides = []
    for rating, name in ((white_rating, white_name), (black_rating, black_name)):
        prior = None
        acc = accounts_for(name)
        if acc:
            prior = OpeningPrior.build(games if games is not None else load_games(), acc)
        sides.append(Side(band_for(rating), prior))
    return Mover(sides[0], sides[1], maia=maia)


# ------------------------------------------------------------------------ fit

def _sample_moves(games: list[Game], n_per_side: int, seed: int, min_ply: int = 0,
                  max_ply: int = 120):
    """(board, played uci, mover rating) samples, a few per side per game."""
    rng = random.Random(seed)
    out = []
    for g in games:
        plies = list(range(min_ply, min(len(g.moves), max_ply)))
        if not plies:
            continue
        for color in (chess.WHITE, chess.BLACK):
            own = [i for i in plies if (i % 2 == 0) == (color == chess.WHITE)]
            pick = set(rng.sample(own, min(n_per_side, len(own))))
            rating = g.white_rating if color == chess.WHITE else g.black_rating
            if rating is None:
                continue
            board = chess.Board()
            for i, uci in enumerate(g.moves):
                if i > max(pick, default=-1):
                    break
                if i in pick and board.legal_moves.count() > 1:
                    out.append((board.copy(stack=False), uci, rating))
                board.push_uci(uci)
    return out


def fit(cutoff: str, *, prior_split: str, n_per_side: int = 6, seed: int = 0,
        out: Path = CONFIG) -> dict:
    """Fit the rating offset and the prior strength on games before ``cutoff``.

    Offset: every sampled move of chess.com rapid games before the cut-off is
    scored under every band; the offset maximises the summed log-likelihood when
    each mover gets ``band_for(rating + offset)``. Prior strength ``k``: the
    prior is built from the owner's games before ``prior_split`` and scored on
    their moves in ``[prior_split, cutoff)``.
    """
    games = load_games()
    rapid = [g for g in games if g.platform == "chesscom" and g.date < cutoff
             and (g.base_s or 0) >= 600]
    samples = _sample_moves(rapid, n_per_side, seed, min_ply=0)
    maia = Maia()
    t0 = time.time()
    ll = []      # per sample: {band: log p}
    for board, uci, rating in samples:
        ll.append({b: math.log(maia.policy(board, b).get(uci, P_FLOOR)) for b in BANDS})
    by_bucket: dict[int, dict[int, float]] = defaultdict(lambda: defaultdict(float))
    counts: Counter = Counter()
    for (board, uci, rating), row in zip(samples, ll):
        bucket = int(rating // 100 * 100)
        counts[bucket] += 1
        for b, v in row.items():
            by_bucket[bucket][b] += v
    offsets = list(range(-400, 801, 50))
    score = {}
    for off in offsets:
        score[off] = sum(row[band_for(r, off)] for (_, _, r), row in zip(samples, ll))
    best_off = max(offsets, key=lambda o: (score[o], -abs(o)))
    per_bucket = {str(k): {"moves": counts[k], "best_band": max(v, key=v.get)}
                  for k, v in sorted(by_bucket.items())}

    # owner's prior strength
    owner = _owner(games)
    hist = OpeningPrior.build(games, owner, before=prior_split)
    test = [g for g in games if prior_split <= g.date < cutoff and (owner & {g.white, g.black})]
    ks = [0.5, 1, 2, 5, 10, 20, 50, 1e9]
    kll = {k: 0.0 for k in ks}
    n_moves = 0
    for g in test:
        side = chess.WHITE if g.white in owner else chess.BLACK
        rating = g.white_rating if side == chess.WHITE else g.black_rating
        if g.platform == "lichess" and rating is not None:
            rating = rating - best_off          # Lichess rating is already on Maia's scale
        band = band_for(rating or 1350, best_off)
        board = chess.Board()
        for uci in g.moves[:40]:
            if board.turn == side and position_key(board) in hist.counts:
                mp = maia.policy(board, band)
                for k in ks:
                    kll[k] += math.log(max(hist.blend(board, mp, k).get(uci, P_FLOOR), 1e-12))
                n_moves += 1
            board.push_uci(uci)
    best_k = max(ks, key=lambda k: kll[k])
    maia.close()
    cfg = {
        "cutoff": cutoff, "prior_split": prior_split, "offset": best_off, "k": best_k,
        "fit": {"samples": len(samples), "games": len(rapid), "secs": round(time.time() - t0, 1),
                "loglik_per_move": {str(o): round(score[o] / len(samples), 4) for o in offsets},
                "per_bucket": per_bucket,
                "prior_moves": n_moves,
                "prior_loglik_per_move": {str(k): round(v / max(n_moves, 1), 4) for k, v in kll.items()}},
    }
    out.write_text(json.dumps(cfg, indent=1) + "\n")
    return cfg


def _owner(games: list[Game]) -> set[str]:
    """The database owner's accounts: the usernames that appear in the most games."""
    c = Counter()
    for g in games:
        c[(g.platform, g.white)] += 1
        c[(g.platform, g.black)] += 1
    out = set()
    for plat in ("chesscom", "lichess"):
        top = max(((n, u) for (p, u), n in c.items() if p == plat), default=None)
        if top:
            out.add(top[1])
    return out


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fit", help="fit rating offset and prior strength")
    f.add_argument("--cutoff", default="2025-07-01")
    f.add_argument("--prior-split", default="2024-07-01")
    f.add_argument("--per-side", type=int, default=6)
    p = sub.add_parser("policy", help="print the policy at a FEN")
    p.add_argument("fen")
    p.add_argument("--band", type=int, default=1500)
    a = ap.parse_args(argv)
    if a.cmd == "fit":
        cfg = fit(a.cutoff, prior_split=a.prior_split, n_per_side=a.per_side)
        print(json.dumps({k: v for k, v in cfg.items() if k != "fit"}))
        print(json.dumps(cfg["fit"], indent=1))
    else:
        b = chess.Board(a.fen)
        pol = Maia().policy(b, a.band)
        for u, v in sorted(pol.items(), key=lambda kv: -kv[1])[:10]:
            print(f"{b.san(chess.Move.from_uci(u)):8s} {v:.4f}")


if __name__ == "__main__":
    main()
