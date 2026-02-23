import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True)
class Hero:
    id: int
    name: str
    aliases: List[str]


class StaticDraftEngine:
    """
    Offline engine:
      - heroes.json: all heroes with ids + names + aliases
      - static_patch.json: meta/synergy/counter + optional roles + optional phase_profiles

    roles format:
      "roles": {
        "129": {"1": 0.05, "2": 0.10, "3": 0.90, "4": 0.20, "5": 0.05},
        ...
      }

    phase_profiles format:
      "phase_profiles": {
        "1": {"w_meta": 1.25, "w_syn": 1.15, "w_cnt": 0.90, "w_role": 1.10},
        "2": {"w_meta": 1.00, "w_syn": 1.20, "w_cnt": 1.20, "w_role": 1.20},
        "3": {"w_meta": 0.80, "w_syn": 1.10, "w_cnt": 1.55, "w_role": 1.25}
      }
    """

    def __init__(self):
        self.heroes: Dict[int, Hero] = {}
        self.name_to_id: Dict[str, int] = {}

        self.meta: Dict[int, float] = {}
        self.synergy: Dict[Tuple[int, int], float] = {}
        self.counter: Dict[Tuple[int, int], float] = {}

        self.roles: Dict[int, Dict[int, float]] = {}
        self.phase_profiles: Dict[int, Dict[str, float]] = {}

        self.patch_name: str = "unknown"

        self._load_heroes()
        self._load_patch()

    def _load_heroes(self):
        p = DATA_DIR / "heroes.json"
        if not p.exists():
            raise RuntimeError("data/heroes.json not found")

        data = json.loads(p.read_text(encoding="utf-8"))
        for h in data:
            hero = Hero(
                id=int(h["id"]),
                name=str(h["name"]),
                aliases=list(h.get("aliases", [])),
            )
            self.heroes[hero.id] = hero

            self.name_to_id[hero.name.lower()] = hero.id
            for a in hero.aliases:
                self.name_to_id[str(a).lower()] = hero.id

    def _load_patch(self):
        p = DATA_DIR / "static_patch.json"
        if not p.exists():
            raise RuntimeError("data/static_patch.json not found")

        data = json.loads(p.read_text(encoding="utf-8"))
        self.patch_name = str(data.get("patch", "manual"))

        # meta
        self.meta = {int(k): float(v) for k, v in (data.get("meta") or {}).items()}

        # synergy: "a:b"
        self.synergy = {}
        for k, v in (data.get("synergy") or {}).items():
            a, b = k.split(":")
            a, b = int(a), int(b)
            if a > b:
                a, b = b, a
            self.synergy[(a, b)] = float(v)

        # counter: "h:e"
        self.counter = {}
        for k, v in (data.get("counter") or {}).items():
            h, e = k.split(":")
            self.counter[(int(h), int(e))] = float(v)

        # roles (optional)
        self.roles = {}
        roles_block = data.get("roles") or {}
        if isinstance(roles_block, dict):
            for hid_str, pos_map in roles_block.items():
                try:
                    hid = int(hid_str)
                except Exception:
                    continue
                if not isinstance(pos_map, dict):
                    continue
                self.roles[hid] = {int(pos): float(score) for pos, score in pos_map.items()}

        # phase profiles (optional) with sane defaults
        defaults = {
            1: {"w_meta": 1.20, "w_syn": 1.15, "w_cnt": 0.95, "w_role": 1.10},
            2: {"w_meta": 1.00, "w_syn": 1.20, "w_cnt": 1.20, "w_role": 1.20},
            3: {"w_meta": 0.85, "w_syn": 1.10, "w_cnt": 1.50, "w_role": 1.25},
        }
        self.phase_profiles = dict(defaults)

        pp = data.get("phase_profiles")
        if isinstance(pp, dict):
            for ph_str, w in pp.items():
                try:
                    ph = int(ph_str)
                except Exception:
                    continue
                if not isinstance(w, dict):
                    continue
                base = defaults.get(ph, defaults[2])
                self.phase_profiles[ph] = {
                    "w_meta": float(w.get("w_meta", base["w_meta"])),
                    "w_syn": float(w.get("w_syn", base["w_syn"])),
                    "w_cnt": float(w.get("w_cnt", base["w_cnt"])),
                    "w_role": float(w.get("w_role", base["w_role"])),
                }

    def resolve(self, text: str) -> Optional[int]:
        t = (text or "").strip().lower()
        if not t:
            return None
        return self.name_to_id.get(t)

    @staticmethod
    def _pair(a: int, b: int) -> Tuple[int, int]:
        return (a, b) if a < b else (b, a)

    def role_score(self, hero_id: int, pos: Optional[int]) -> float:
        """
        pos: None=Any, else 1..5
        returns 0..1
        """
        if pos is None:
            return 0.0
        return float((self.roles.get(hero_id) or {}).get(int(pos), 0.0))

    def recommend(
            self,
            ally: List[int],
            enemy: List[int],
            banned: List[int],
            *,
            top_n: int = 10,
            phase: int = 2,
            pos: Optional[int] = None,
    ):
        picked = set(ally) | set(enemy) | set(banned)
        out = []

        weights = self.phase_profiles.get(int(phase), self.phase_profiles[2])
        w_meta = weights["w_meta"]
        w_syn = weights["w_syn"]
        w_cnt = weights["w_cnt"]
        w_role = weights["w_role"]

        for hid, hero in self.heroes.items():
            if hid in picked:
                continue

            meta = self.meta.get(hid, 0.0)

            syn = 0.0
            for a in ally:
                syn += self.synergy.get(self._pair(hid, a), 0.0)

            cnt = 0.0
            for e in enemy:
                cnt += self.counter.get((hid, e), 0.0)

            rscore = self.role_score(hid, pos)
            score = (w_meta * meta + w_syn * syn + w_cnt * cnt) + (w_role * rscore)

            out.append({
                "id": hid,
                "name": hero.name,
                "score": float(score),
                "meta": float(meta),
                "syn": float(syn),
                "cnt": float(cnt),
                "role": float(rscore),
            })

        out.sort(key=lambda x: x["score"], reverse=True)
        return out[:top_n]