from __future__ import annotations

import os
import io
import re
import json
import logging
import math as _math
import textwrap as _textwrap
from dataclasses import dataclass, field as _field
from typing import Optional as _Optional

import aiohttp
import emoji as emoji_lib
import discord
from discord import app_commands
from discord.ext import commands, tasks
from PIL import Image, ImageDraw, ImageFont

# Autocomplete responses can occasionally arrive after Discord has already invalidated
# the interaction (e.g. the user typed another character before the bot replied).
# discord.py already handles this gracefully — it just logs a full traceback as noise.
# Filter that specific, harmless message out so real errors aren't buried under it.
class _SuppressAutocompleteRaceNoise(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        return "Ignoring exception in autocomplete" not in record.getMessage()


logging.getLogger("discord.app_commands.tree").addFilter(_SuppressAutocompleteRaceNoise())

# ---------- Config ----------
CONFIRM_CHANNEL_ID = 1528146431138074624   # admins confirm new teams here
TEAM_CATEGORY_ID = 1528146975554404552     # category new team channels are created in
LOG_CHANNEL_ID = 1528147225799037008       # single JSON "database" message lives here
REFERENCE_ROLE_ID = 1528009686509420616    # team roles are kept positioned just above this role
STAFF_ROLE_ID = 1528009567219224616        # only holders of this role can use staff team-management commands
PREMIUM_ROLE_ID = 1528139462159106059      # gates /premiumteamsettings; premium team roles are kept above this role
PREMIUM_ROLE_ID_2 = 1529805001088569384    # a second role that also grants premium access
CREATE_TEAM_ROLE_ID = 1528160422857932868  # required to use /createteam (pre-existing teams are grandfathered in)
TEAM_LEADER_ROLE_ID = 1528445357317423135  # granted to every team leader, current and future
MAX_TEAM_MEMBERS = 20                      # includes the leader
SUPPORT_TICKET_CHANNEL_ID = 1528355152287760405  # the support ticket panel is posted/refreshed here
TOURNAMENT_PANEL_CHANNEL_ID = 1528515043992404150  # the tournament team-select panel is posted/refreshed here
BRACKET_PANEL_CHANNEL_ID = 1529840551049040025   # the bracket control panel lives here
BRACKET_PUBLIC_CHANNEL_ID = 1529826078787637320  # the public bracket image is posted/edited here
OCULUS_UPDATE_CHANNEL_ID = 1528008387420356629  # where Animal Company update announcements post
OCULUS_APP_ID = "7190422614401072"  # Animal Company's OculusDB app ID
OCULUS_VERSIONS_URL = f"https://oculusdb.rui2015.me/api/v1/versions/{OCULUS_APP_ID}?onlydownloadable=true"
OCULUS_VERSION_FILE = "oculus_version.json"  # tracks the last version we've already announced
META_UPDATE_EMOJI = "<:Meta:1528228318510452786>"

DB_FILE = "teams.json"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SUPPORT_BANNER_PATH = os.path.join(BASE_DIR, "support_banner.png")
SUPPORT_BANNER_FILENAME = "support_banner.png"
BRACKET_IMAGE_PATH = os.path.join(BASE_DIR, "bracket.png")
BRACKET_IMAGE_FILENAME = "bracket.png"


# ============================================================
# BRACKET — State, image generation, and DB helpers
# ============================================================

BRACKET_TOTAL_SLOTS = 8
BRACKET_ROUNDS = 3  # round1 (4 matches) → round2 (2) → finals (1)

# Bracket image layout
IMG_PAD_LEFT = 50
IMG_PAD_TOP = 110
IMG_PAD_BOTTOM = 50
IMG_PAD_RIGHT = 70
SEED_BADGE_D = 24     # diameter of the little seed-number circle to the left of round-1 boxes
BOX_W = 190
BOX_H = 42
BOX_GAP = 12
ROUND_GAP = 300
LABEL_H = 24
ROUND_LABEL_H = 34

# Bracket colours (RGB) — layered dark theme with a gold accent for anything "live" or won
COL_BG_TOP = (21, 20, 34)
COL_BG_BOTTOM = (13, 13, 22)
COL_VIGNETTE = (0, 0, 0)
COL_ROUND_LABEL = (200, 190, 230)
COL_ROUND_PILL = (46, 40, 72)
COL_ROUND_PILL_BORDER = (90, 80, 130)
COL_MATCH_LABEL = (130, 128, 155)
COL_SLOT_EMPTY_BG = (35, 34, 48)
COL_SLOT_TEAM_BG = (46, 45, 68)
COL_SLOT_BORDER = (70, 68, 98)
COL_READY_BG = (54, 48, 40)
COL_READY_BORDER = (240, 185, 70)
COL_WINNER_BG_TOP = (44, 150, 92)
COL_WINNER_BG_BOTTOM = (28, 105, 66)
COL_WINNER_BORDER = (120, 235, 165)
COL_CHAMPION_BORDER = (255, 205, 60)
COL_TEXT_EMPTY = (95, 93, 112)
COL_TEXT_TEAM = (232, 230, 245)
COL_TEXT_WINNER = (255, 255, 255)
COL_TEXT_PLACEHOLDER = (110, 108, 135)
COL_LINE = (95, 92, 125)
COL_LINE_WON = (245, 195, 80)
COL_SEED_BG = (60, 58, 88)
COL_SEED_TEXT = (200, 198, 225)
COL_SHADOW = (5, 5, 10)

FONT_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]

_font_cache: dict = {}


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_colour(c1: tuple, c2: tuple, t: float) -> tuple:
    return tuple(int(_lerp(c1[i], c2[i], t)) for i in range(3))


def _get_font(size: int, bold: bool = True):
    key = ("bold" if bold else "regular", size)
    if key in _font_cache:
        return _font_cache[key]
    for path in FONT_PATHS:
        if os.path.exists(path):
            font = ImageFont.truetype(path, size)
            _font_cache[key] = font
            return font
    font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


@dataclass
class BracketState:
    """Holds the full state of a single-elimination bracket (8 slots, 3 rounds)."""
    title: str = "Tournament Bracket"
    participants: list = _field(default_factory=lambda: ["N/A"] * BRACKET_TOTAL_SLOTS)
    matches: list = _field(default_factory=lambda: [
        [{"winner": None} for _ in range(4)],
        [{"winner": None} for _ in range(2)],
        [{"winner": None}],
    ])
    # Ordered list of [round, match] entries recording every winner ever set, so the most
    # recent result can be popped off and reverted with "Undo Last Result".
    history: list = _field(default_factory=list)

    def _matches_in_round(self, r: int) -> int:
        return 4 >> r

    def _team_in_match(self, r: int, m: int, slot: int) -> str:
        if r == 0:
            idx = m * 2 + slot
            return self.participants[idx] if idx < len(self.participants) else "N/A"
        prev = self.matches[r - 1][m * 2 + slot]
        winner = prev.get("winner")
        if winner is not None:
            return self.participants[winner] if winner < len(self.participants) else "N/A"
        return "TBD"

    def match_label(self, r: int, m: int) -> str:
        if r == 2:
            return "Final"
        return f"Match {m + 1}/{self._matches_in_round(r)}"

    def current_round(self) -> int:
        for r in range(BRACKET_ROUNDS):
            for match in self.matches[r]:
                if match.get("winner") is None:
                    return r
        return BRACKET_ROUNDS - 1

    def playable_matches(self) -> list:
        r = self.current_round()
        out = []
        for m in range(self._matches_in_round(r)):
            match = self.matches[r][m]
            if match.get("winner") is not None:
                continue
            team_a = self._team_in_match(r, m, 0)
            team_b = self._team_in_match(r, m, 1)
            if team_a not in ("N/A", "TBD") and team_b not in ("N/A", "TBD"):
                out.append((r, m))
        return out

    def set_winner(self, r: int, m: int, winner_slot: int) -> bool:
        if r < 0 or r >= BRACKET_ROUNDS:
            return False
        if m < 0 or m >= self._matches_in_round(r):
            return False
        match = self.matches[r][m]
        if match.get("winner") is not None:
            return False
        if winner_slot not in (0, 1):
            return False
        team_a = self._team_in_match(r, m, 0)
        team_b = self._team_in_match(r, m, 1)
        if team_a in ("N/A", "TBD") or team_b in ("N/A", "TBD"):
            return False

        if r == 0:
            winner_idx = m * 2 + winner_slot
        else:
            chain_match = m * 2 + winner_slot
            prev_winner = self.matches[r - 1][chain_match].get("winner")
            if prev_winner is None:
                return False
            winner_idx = prev_winner

        match["winner"] = winner_idx
        if r + 1 < BRACKET_ROUNDS:
            next_match = m // 2
            self.matches[r + 1][next_match] = {"winner": None}
        self.history.append([r, m])
        return True

    def undo_last(self):
        """Reverts the most recently set winner. Also clears any later-round match that had
        already cascaded from it (e.g. undoing a semi-final result also clears the final if
        it had already been decided using that semi's winner). Returns the (round, match)
        that was undone, or None if there was nothing to undo."""
        if not self.history:
            return None
        r, m = self.history.pop()
        self.matches[r][m] = {"winner": None}

        cur_r, cur_m = r, m
        while cur_r + 1 < BRACKET_ROUNDS:
            next_r = cur_r + 1
            next_m = cur_m // 2
            if self.matches[next_r][next_m].get("winner") is not None:
                self.matches[next_r][next_m] = {"winner": None}
                self.history = [entry for entry in self.history if entry != [next_r, next_m]]
                cur_r, cur_m = next_r, next_m
            else:
                break
        return (r, m)

    def clear_after(self, slot: int) -> list:
        cleared = []
        m0 = slot // 2
        if self.matches[0][m0].get("winner") is not None:
            self.matches[0][m0] = {"winner": None}
            cleared.append((0, m0))
        m1 = m0 // 2
        if self.matches[1][m1].get("winner") is not None:
            self.matches[1][m1] = {"winner": None}
            cleared.append((1, m1))
        if self.matches[2][0].get("winner") is not None:
            self.matches[2][0] = {"winner": None}
            cleared.append((2, 0))
        return cleared

    def clear_all(self):
        self.participants = ["N/A"] * BRACKET_TOTAL_SLOTS
        self.matches = [
            [{"winner": None} for _ in range(4)],
            [{"winner": None} for _ in range(2)],
            [{"winner": None}],
        ]
        self.history = []


def _draw_gradient_background(img: Image.Image, top: tuple, bottom: tuple) -> None:
    w, h = img.size
    draw = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        draw.line([(0, y), (w, y)], fill=_lerp_colour(top, bottom, t))


def _draw_shadowed_rounded_rect(draw, box, radius, fill, outline, width, shadow_offset=(0, 3)):
    sx, sy = shadow_offset
    shadow_box = [box[0] + sx, box[1] + sy, box[2] + sx, box[3] + sy]
    draw.rounded_rectangle(shadow_box, radius=radius, fill=COL_SHADOW)
    draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline, width=width)


def _draw_confetti(draw, w, h, count, rng):
    palette = [
        (255, 205, 60), (240, 90, 90), (90, 200, 255), (140, 230, 140), (220, 130, 255),
    ]
    for _ in range(count):
        x = rng.uniform(0, w)
        y = rng.uniform(0, h)
        size = rng.uniform(3, 7)
        colour = palette[int(rng.uniform(0, len(palette)))]
        if rng.random() < 0.5:
            draw.ellipse([x, y, x + size, y + size], fill=colour)
        else:
            draw.rectangle([x, y, x + size, y + size * 0.6], fill=colour)


def _draw_bracket_image(state: BracketState) -> Image.Image:
    import random

    max_per_round = max(state._matches_in_round(r) for r in range(BRACKET_ROUNDS))
    img_w = IMG_PAD_LEFT + BRACKET_ROUNDS * BOX_W + (BRACKET_ROUNDS - 1) * ROUND_GAP + IMG_PAD_RIGHT + SEED_BADGE_D
    img_h = IMG_PAD_TOP + max_per_round * (2 * BOX_H + BOX_GAP + LABEL_H) + IMG_PAD_BOTTOM + ROUND_LABEL_H

    champion = None
    final_match = state.matches[2][0]
    if final_match.get("winner") is not None:
        idx = final_match["winner"]
        if idx < len(state.participants):
            champion = state.participants[idx]

    img = Image.new("RGB", (img_w, img_h), COL_BG_BOTTOM)
    _draw_gradient_background(img, COL_BG_TOP, COL_BG_BOTTOM)
    draw = ImageDraw.Draw(img)

    font_label = _get_font(12, bold=False)
    font_team = _get_font(15, bold=True)
    font_empty = _get_font(13, bold=False)
    font_round = _get_font(15, bold=True)
    font_title = _get_font(26, bold=True)
    font_seed = _get_font(11, bold=True)
    font_champion = _get_font(30, bold=True)

    # --- Title banner ---
    title_y = 30 if not champion else 26
    draw.text((img_w / 2, title_y), state.title, fill=(245, 244, 250), font=font_title, anchor="mm")

    if champion:
        rng = random.Random(hash(champion) & 0xFFFFFFFF)
        _draw_confetti(draw, img_w, img_h, 90, rng)
        band_h = 30
        band_y = 58
        draw.rectangle([0, band_y, img_w, band_y + band_h], fill=(40, 32, 10))
        draw.text(
            (img_w / 2, band_y + band_h / 2),
            f"🏆  CHAMPION — {champion}  🏆",
            fill=COL_CHAMPION_BORDER, font=font_champion if img_w > 500 else font_round, anchor="mm",
        )

    # --- Round headers as pills ---
    round_names = {0: "Round 1", 1: "Round 2", 2: "Finals"}
    for r in range(BRACKET_ROUNDS):
        x = IMG_PAD_LEFT + SEED_BADGE_D + r * (BOX_W + ROUND_GAP)
        label = round_names[r]
        bbox = draw.textbbox((0, 0), label, font=font_round)
        pill_w = (bbox[2] - bbox[0]) + 36
        pill_x = x + BOX_W / 2 - pill_w / 2
        pill_y = IMG_PAD_TOP - ROUND_LABEL_H + 4
        draw.rounded_rectangle(
            [pill_x, pill_y, pill_x + pill_w, pill_y + 24],
            radius=12, fill=COL_ROUND_PILL, outline=COL_ROUND_PILL_BORDER, width=1,
        )
        draw.text((x + BOX_W / 2, pill_y + 12), label, fill=COL_ROUND_LABEL, font=font_round, anchor="mm")

    positions = []

    for r in range(BRACKET_ROUNDS):
        n = state._matches_in_round(r)
        total_h = n * (2 * BOX_H + BOX_GAP + LABEL_H)
        y_offset = IMG_PAD_TOP + ROUND_LABEL_H
        round_positions = []

        for m in range(n):
            x = IMG_PAD_LEFT + SEED_BADGE_D + r * (BOX_W + ROUND_GAP)
            y = y_offset + m * (2 * BOX_H + BOX_GAP + LABEL_H)

            draw.text(
                (x + BOX_W // 2, y + 6),
                state.match_label(r, m).upper(), fill=COL_MATCH_LABEL, font=font_label, anchor="mm",
            )

            team_a = state._team_in_match(r, m, 0)
            team_b = state._team_in_match(r, m, 1)
            winner = state.matches[r][m].get("winner")
            match_ready = (
                winner is None and team_a not in ("N/A", "TBD") and team_b not in ("N/A", "TBD")
            )

            for slot, (team, box_y) in enumerate([(team_a, y + LABEL_H), (team_b, y + LABEL_H + BOX_H + BOX_GAP)]):
                is_empty = team == "N/A"
                is_tbd = team == "TBD"
                is_winner = winner is not None and (
                    winner < len(state.participants) and state.participants[winner] == team
                )
                is_champion_box = is_winner and r == 2

                if is_winner:
                    # Vertical mini-gradient fill for winner boxes for a bit of shine
                    grad = Image.new("RGB", (BOX_W, BOX_H))
                    _draw_gradient_background(grad, COL_WINNER_BG_TOP, COL_WINNER_BG_BOTTOM)
                    mask = Image.new("L", (BOX_W, BOX_H), 0)
                    mdraw = ImageDraw.Draw(mask)
                    mdraw.rounded_rectangle([0, 0, BOX_W, BOX_H], radius=8, fill=255)
                    draw.rounded_rectangle(
                        [x + 2, box_y + 5, x + BOX_W + 2, box_y + BOX_H + 5], radius=8, fill=COL_SHADOW,
                    )
                    img.paste(grad, (int(x), int(box_y)), mask)
                    border = COL_CHAMPION_BORDER if is_champion_box else COL_WINNER_BORDER
                    draw.rounded_rectangle(
                        [x, box_y, x + BOX_W, box_y + BOX_H], radius=8, outline=border,
                        width=3 if is_champion_box else 2,
                    )
                    text_col = COL_TEXT_WINNER
                elif match_ready:
                    _draw_shadowed_rounded_rect(
                        draw, [x, box_y, x + BOX_W, box_y + BOX_H], 8,
                        COL_READY_BG, COL_READY_BORDER, 2,
                    )
                    text_col = COL_TEXT_TEAM
                elif is_empty or is_tbd:
                    _draw_shadowed_rounded_rect(
                        draw, [x, box_y, x + BOX_W, box_y + BOX_H], 8,
                        COL_SLOT_EMPTY_BG, COL_SLOT_BORDER, 1,
                    )
                    text_col = COL_TEXT_PLACEHOLDER if is_tbd else COL_TEXT_EMPTY
                else:
                    _draw_shadowed_rounded_rect(
                        draw, [x, box_y, x + BOX_W, box_y + BOX_H], 8,
                        COL_SLOT_TEAM_BG, COL_SLOT_BORDER, 1,
                    )
                    text_col = COL_TEXT_TEAM

                # Seed badge for round 1 boxes with a real team in them
                text_x = x + 12
                if r == 0 and not is_empty and not is_tbd:
                    seed_no = m * 2 + slot + 1
                    bx = x - SEED_BADGE_D - 6
                    by = box_y + (BOX_H - SEED_BADGE_D) / 2
                    draw.ellipse(
                        [bx, by, bx + SEED_BADGE_D, by + SEED_BADGE_D],
                        fill=COL_SEED_BG, outline=COL_SLOT_BORDER, width=1,
                    )
                    draw.text(
                        (bx + SEED_BADGE_D / 2, by + SEED_BADGE_D / 2),
                        str(seed_no), fill=COL_SEED_TEXT, font=font_seed, anchor="mm",
                    )

                display = "N/A" if is_empty else ("TBD" if is_tbd else _truncate(team, 18))
                font_use = font_empty if (is_empty or is_tbd) else font_team

                text_bbox = draw.textbbox((0, 0), display, font=font_use)
                text_h = text_bbox[3] - text_bbox[1]
                ty = box_y + (BOX_H - text_h) / 2
                draw.text((text_x, ty), display, fill=text_col, font=font_use)

                if is_winner:
                    trophy = "🏆" if is_champion_box else "✓"
                    draw.text(
                        (x + BOX_W - 20, box_y + BOX_H / 2), trophy,
                        fill=COL_CHAMPION_BORDER if is_champion_box else COL_TEXT_WINNER,
                        font=font_team, anchor="mm",
                    )

            round_positions.append((x, y, BOX_W, total_h))
        positions.append(round_positions)

    for r in range(BRACKET_ROUNDS - 1):
        n = state._matches_in_round(r)
        for m in range(n):
            x, y, w, _ = positions[r][m]
            team_a = state._team_in_match(r, m, 0)
            team_b = state._team_in_match(r, m, 1)
            a_playable = team_a not in ("N/A", "TBD")
            b_playable = team_b not in ("N/A", "TBD")
            if not a_playable and not b_playable:
                continue

            winner = state.matches[r][m].get("winner")
            line_col = COL_LINE_WON if winner is not None else COL_LINE
            line_w = 3 if winner is not None else 2

            box_bottom_a = y + LABEL_H + BOX_H
            box_bottom_b = y + LABEL_H + BOX_H + BOX_GAP + BOX_H
            mid_y = (box_bottom_a + box_bottom_b) / 2

            nx, ny, _, _ = positions[r + 1][m // 2]
            next_box_y = ny + LABEL_H + (BOX_H if (m % 2 == 1) else 0) + BOX_GAP * (m % 2)
            next_mid_y = next_box_y + BOX_H / 2

            elbow_x = x + w + 30
            joint_r = 3

            if a_playable:
                draw.line([(x + w, box_bottom_a), (elbow_x, box_bottom_a)], fill=line_col, width=line_w)
            if b_playable:
                draw.line([(x + w, box_bottom_b), (elbow_x, box_bottom_b)], fill=line_col, width=line_w)
            if a_playable or b_playable:
                draw.line([(elbow_x, mid_y), (elbow_x, next_mid_y)], fill=line_col, width=line_w)
                draw.line([(elbow_x, next_mid_y), (nx, next_mid_y)], fill=line_col, width=line_w)
                draw.ellipse(
                    [elbow_x - joint_r, next_mid_y - joint_r, elbow_x + joint_r, next_mid_y + joint_r],
                    fill=line_col,
                )

    return img


def generate_bracket_image(state: BracketState, path: str) -> str:
    img = _draw_bracket_image(state)
    img.save(path, "PNG")
    return path


def load_bracket(db: dict):
    b = db.get("bracket")
    if not b:
        return None
    return BracketState(
        title=b.get("title", "Tournament Bracket"),
        participants=b.get("participants", ["N/A"] * BRACKET_TOTAL_SLOTS),
        matches=b.get("matches", [
            [{"winner": None} for _ in range(4)],
            [{"winner": None} for _ in range(2)],
            [{"winner": None}],
        ]),
        history=[list(entry) for entry in b.get("history", [])],
    )


def save_bracket(db: dict, state: BracketState) -> None:
    db["bracket"] = {
        "title": state.title,
        "participants": state.participants,
        "matches": state.matches,
        "history": state.history,
    }


# ---------- Bot setup ----------
intents = discord.Intents.default()
intents.members = True  # needed to reliably resolve members / add roles

bot = commands.Bot(command_prefix="!", intents=intents)


# ---------- JSON "database" helpers ----------
def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        return {"teams": {}, "giveaways": {}}
    with open(DB_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if "teams" not in data:
        # migrate old flat-format {team_name: {...}} files
        data = {"teams": data}
    data.setdefault("teams", {})
    data.setdefault("giveaways", {})
    return data


def save_db(data: dict) -> None:
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# ---------- Meta Quest Store update tracker (Animal Company, via OculusDB's public API) ----------
def load_oculus_version_state() -> dict:
    """Load persisted version state. Returns {"current_version_code": ..., "previous_display_version": ...}.
    Backward-compatible with older files that stored "last_version_code" or "previous_version_code"."""
    if not os.path.exists(OCULUS_VERSION_FILE):
        return {"current_version_code": None, "previous_display_version": None}
    try:
        with open(OCULUS_VERSION_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return {
                "current_version_code": data.get("current_version_code") or data.get("last_version_code"),
                "previous_display_version": data.get("previous_display_version")
                or data.get("previous_version_code")
                or data.get("last_display_version"),
            }
        return {"current_version_code": None, "previous_display_version": None}
    except (json.JSONDecodeError, OSError):
        return {"current_version_code": None, "previous_display_version": None}


def save_oculus_version_state(current_version_code, previous_display_version=None) -> None:
    with open(OCULUS_VERSION_FILE, "w", encoding="utf-8") as f:
        json.dump(
            {
                "current_version_code": current_version_code,
                "previous_display_version": previous_display_version,
            },
            f,
        )


def _extract_latest_oculus_version_entry(payload):
    """Confirmed against a live response: OculusDB's /api/v1/versions endpoint returns a bare
    JSON list of version objects, newest first. Still guards against an empty list or an
    unexpected wrapper dict in case the API shape ever changes."""
    if isinstance(payload, list):
        entries = payload
    elif isinstance(payload, dict):
        entries = payload.get("versions") or payload.get("data") or payload.get("items") or []
    else:
        entries = []
    return entries[0] if entries else None


def _extract_oculus_version_code(entry: dict):
    """versionCode (e.g. 3211) is the reliable field for detecting an actual new build —
    confirmed present on every entry in a live response."""
    return entry.get("versionCode")


def _extract_oculus_display_version(entry: dict):
    """version (e.g. "1.82.2.3211") is the human-readable string shown in the announcement.
    Falls back to the version code if it's ever missing."""
    return entry.get("version") or entry.get("versionCode")


def _extract_release_timestamp(entry: dict):
    """Returns a Unix epoch int from the entry's release/upload date, or the current time."""
    from datetime import datetime, timezone
    ts = entry.get("uploadDate") or entry.get("lastPublishedDate") or entry.get("created")
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except (ValueError, TypeError):
            pass
    return int(datetime.now(timezone.utc).timestamp())


def _build_update_embed(display_version, previous_version=None, release_ts=None):
    """Builds the 'Meta Update Detected' embed matching the AC: Arena Hub style:
    title, a Meta-emoji header, updated/last-logged version blocks, time of release,
    and the support banner image — with a live "Checked at ... UTC" footer/timestamp."""
    from datetime import datetime, timezone

    embed = discord.Embed(
        title="AC: Arena Hub",
        description=f"{META_UPDATE_EMOJI} **Meta Update Detected**",
        colour=discord.Colour.orange(),
    )

    embed.add_field(
        name="🟢 Updated Version:",
        value=f"```\n{display_version}\n```",
        inline=False,
    )

    embed.add_field(
        name="🔴 Last Logged:",
        value=f"```\n{previous_version if previous_version else 'N/A'}\n```",
        inline=False,
    )

    if release_ts:
        embed.add_field(
            name="🕒 Time of Live Release:",
            value=f"<t:{release_ts}:F>\n(<t:{release_ts}:R>)",
            inline=False,
        )

    if os.path.exists(SUPPORT_BANNER_PATH):
        embed.set_image(url=f"attachment://{SUPPORT_BANNER_FILENAME}")

    checked_at = datetime.now(timezone.utc)
    embed.timestamp = checked_at
    embed.set_footer(text=f"Checked at {checked_at.strftime('%Y-%m-%d %H:%M:%S')} UTC")
    return embed


@tasks.loop(minutes=10)
async def check_oculus_updates():
    if not OCULUS_UPDATE_CHANNEL_ID:
        return  # not configured yet

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                OCULUS_VERSIONS_URL, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    print(f"Oculus update check failed: HTTP {response.status}")
                    return
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as e:
        print(f"Oculus update check failed: {e}")
        return

    latest_entry = _extract_latest_oculus_version_entry(payload)
    if latest_entry is None:
        print("Oculus update check: no version entries found in the response — API shape may have changed.")
        return

    latest_version_code = _extract_oculus_version_code(latest_entry)
    if latest_version_code is None:
        print(f"Oculus update check: couldn't find versionCode in entry: {latest_entry}")
        return

    state = load_oculus_version_state()
    last_seen_code = state["current_version_code"]

    if last_seen_code is None:
        # First run — record baseline without announcing, so a restart never looks like a
        # fake "update".
        display_version = _extract_oculus_display_version(latest_entry)
        save_oculus_version_state(latest_version_code, display_version)
        return

    if str(latest_version_code) == str(last_seen_code):
        return  # no change

    display_version = _extract_oculus_display_version(latest_entry)
    release_ts = _extract_release_timestamp(latest_entry)
    previous_display = state.get("previous_display_version") or str(last_seen_code)

    channel = bot.get_channel(OCULUS_UPDATE_CHANNEL_ID) or await bot.fetch_channel(OCULUS_UPDATE_CHANNEL_ID)
    embed = _build_update_embed(
        display_version=display_version,
        previous_version=previous_display,
        release_ts=release_ts,
    )

    file = None
    if os.path.exists(SUPPORT_BANNER_PATH):
        file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)

    if file:
        await channel.send(embed=embed, file=file)
    else:
        await channel.send(embed=embed)

    # The new display version becomes "previous" the next time an update fires.
    save_oculus_version_state(
        current_version_code=latest_version_code,
        previous_display_version=display_version,
    )


@check_oculus_updates.before_loop
async def before_check_oculus_updates():
    await bot.wait_until_ready()


# Cache of the single database message so we edit it in place instead of
# posting a new file every time. Populated lazily by scanning channel history.
_db_message_cache = None

# User IDs with a /createteam request currently awaiting admin confirmation,
# so the same user can't queue up multiple pending requests.
pending_team_requests: set = set()


async def get_or_create_db_message():
    global _db_message_cache
    if _db_message_cache is not None:
        return _db_message_cache

    channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.attachments and msg.attachments[0].filename == DB_FILE:
            _db_message_cache = msg
            return msg
    return None


async def backup_db_to_log_channel():
    """Keeps a single message in the log channel updated with the current database,
    editing it in place rather than posting a new file every time."""
    global _db_message_cache

    channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
    with open(DB_FILE, "rb") as f:
        file_bytes = f.read()
    new_file = discord.File(io.BytesIO(file_bytes), filename=DB_FILE)

    msg = await get_or_create_db_message()
    if msg is not None:
        try:
            edited = await msg.edit(content="📦 Database (auto-updated):", attachments=[new_file])
            _db_message_cache = edited
            return
        except discord.HTTPException:
            pass  # message may have been deleted; fall through and send a fresh one

    sent = await channel.send(content="📦 Database (auto-updated):", file=new_file)
    _db_message_cache = sent


async def restore_db_from_log_channel():
    """Pulls the last known database backup from the log channel into local storage.
    Critical because Railway wipes the container's disk on every redeploy — without this,
    every restart would silently start from an empty database even though a good backup
    is sitting in Discord."""
    global _db_message_cache

    if os.path.exists(DB_FILE):
        return  # local data already present (e.g. a crash-restart, not a fresh container)

    try:
        channel = bot.get_channel(LOG_CHANNEL_ID) or await bot.fetch_channel(LOG_CHANNEL_ID)
        async for msg in channel.history(limit=50):
            if msg.author.id == bot.user.id and msg.attachments and msg.attachments[0].filename == DB_FILE:
                data = await msg.attachments[0].read()
                with open(DB_FILE, "wb") as f:
                    f.write(data)
                _db_message_cache = msg
                print("Restored database from log channel backup.")
                return
        print("No existing database backup found in log channel — starting fresh.")
    except discord.HTTPException as e:
        print(f"Failed to restore database from log channel: {e}")


def find_team_by_leader(db: dict, user_id: int):
    for name, info in db.items():
        if info["leader_id"] == user_id:
            return name
    return None


def find_team_by_member(db: dict, user_id: int):
    for name, info in db.items():
        if user_id in info.get("members", []):
            return name
    return None


def find_team_by_channel(db: dict, channel_id: int):
    for name, info in db.items():
        if info.get("channel_id") == channel_id:
            return name
    return None


def find_team_key_ci(db: dict, name: str):
    name_lower = name.lower()
    for key in db:
        if key.lower() == name_lower:
            return key
    return None


def is_valid_standard_emoji(text: str) -> bool:
    """True only for a single standard/unicode Discord emoji (no custom server emoji,
    no plain text) — custom emoji can't be used in channel names or as role icons this way."""
    return emoji_lib.is_emoji(text)


def normalize_hex_colour(text: str):
    """Returns a '#RRGGBB' string if valid, else None."""
    if re.fullmatch(r"#?[0-9A-Fa-f]{6}", text.strip()):
        cleaned = text.strip().lstrip("#")
        return f"#{cleaned}"
    return None


def has_staff_role(member: discord.Member) -> bool:
    return any(role.id == STAFF_ROLE_ID for role in member.roles)


def has_premium_access(member: discord.Member) -> bool:
    return any(role.id in (PREMIUM_ROLE_ID, PREMIUM_ROLE_ID_2, STAFF_ROLE_ID) for role in member.roles)


def has_create_team_access(member: discord.Member) -> bool:
    return any(role.id == CREATE_TEAM_ROLE_ID for role in member.roles)


def team_leader_channel_overwrite() -> discord.PermissionOverwrite:
    """Permissions granted to a team leader in their own team channel: on top of viewing/
    sending, manage_messages lets them delete and pin messages, and mention_everyone lets
    them ping their team's role even though the role itself isn't set to be mentionable."""
    return discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        manage_messages=True,
        mention_everyone=True,
    )


# Preset palette offered in /premiumteamsettings' colour1/colour2 dropdowns (Discord caps choices at 25).
PREMIUM_COLOUR_CHOICES = [
    app_commands.Choice(name="Red", value="#ED4245"),
    app_commands.Choice(name="Crimson", value="#DC143C"),
    app_commands.Choice(name="Maroon", value="#800000"),
    app_commands.Choice(name="Orange", value="#E67E22"),
    app_commands.Choice(name="Coral", value="#FF7F50"),
    app_commands.Choice(name="Gold", value="#F1C40F"),
    app_commands.Choice(name="Yellow", value="#FEE75C"),
    app_commands.Choice(name="Lime", value="#32CD32"),
    app_commands.Choice(name="Green", value="#57F287"),
    app_commands.Choice(name="Teal", value="#1ABC9C"),
    app_commands.Choice(name="Turquoise", value="#40E0D0"),
    app_commands.Choice(name="Cyan", value="#00FFFF"),
    app_commands.Choice(name="Sky Blue", value="#3498DB"),
    app_commands.Choice(name="Blue", value="#5865F2"),
    app_commands.Choice(name="Navy", value="#2C3E50"),
    app_commands.Choice(name="Indigo", value="#6F2DA8"),
    app_commands.Choice(name="Purple", value="#9B59B6"),
    app_commands.Choice(name="Violet", value="#8F00FF"),
    app_commands.Choice(name="Magenta", value="#FF00FF"),
    app_commands.Choice(name="Pink", value="#EB459E"),
    app_commands.Choice(name="Hot Pink", value="#FF69B4"),
    app_commands.Choice(name="Brown", value="#8B4513"),
    app_commands.Choice(name="Silver", value="#C0C0C0"),
    app_commands.Choice(name="Black", value="#23272A"),
    app_commands.Choice(name="White", value="#FFFFFF"),
]


SUPPORT_PANEL_TITLE = "Discord Support System"


def build_support_ticket_embed() -> discord.Embed:
    description = (
        "Welcome! Before opening a ticket, please read the rules below "
        "carefully. Our team is here to help with server issues.\n\n"
        "## 📘 Ticket Rules\n"
        "`1.` Please follow our server rules and stay respectful.\n"
        "`2.` Do not open a ticket to report in-game issues.\n"
        "`3.` Do not spam or open multiple tickets for the same issue.\n"
        "`4.` Do not use tickets to report bugs, use the proper bug report channel.\n\n"
        "## ⏳ Response Time\n"
        "If you don't respond within 48 hours, your ticket will be closed.\n\n"
        "## 🤔 Need Help With Something Else?\n"
        "<#1528007337699311740>\n"
        "<#1528009356119900210>\n"
        "<#1528230357072347146>"
    )
    embed = discord.Embed(
        title=SUPPORT_PANEL_TITLE,
        description=description,
        colour=discord.Colour.orange(),
    )
    embed.set_image(url=f"attachment://{SUPPORT_BANNER_FILENAME}")
    embed.set_footer(text="Animal Company: Arena Hub")
    return embed


# ---------- Cosmetic dropdown shown under the support ticket panel banner ----------
class SupportPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.select(
        placeholder="Select a category...",
        options=[
            discord.SelectOption(
                label="Discord Issue",
                emoji=discord.PartialEmoji(name="SilverTrophy", id=1528216893297791098),
            ),
            discord.SelectOption(
                label="Report A Discord User",
                emoji=discord.PartialEmoji(name="boombox", id=1528218480657170452),
            ),
        ],
        custom_id="support_panel_category_select",
    )
    async def category_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        # Cosmetic only for now — no ticket creation logic wired up yet.
        await interaction.response.send_message(
            "Ticket creation isn't set up yet — check back soon!", ephemeral=True
        )


async def refresh_support_ticket_panel():
    """Deletes any previously posted support ticket panel in the target channel and
    posts a fresh one. Called on every bot startup so the panel never goes stale or
    duplicates across restarts."""
    channel = bot.get_channel(SUPPORT_TICKET_CHANNEL_ID) or await bot.fetch_channel(SUPPORT_TICKET_CHANNEL_ID)

    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == SUPPORT_PANEL_TITLE:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    view = SupportPanelView()

    if not os.path.exists(SUPPORT_BANNER_PATH):
        print(f"Support banner image missing at {SUPPORT_BANNER_PATH} — panel sent without image.")
        await channel.send(embed=build_support_ticket_embed(), view=view)
        return

    embed = build_support_ticket_embed()
    file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
    await channel.send(embed=embed, file=file, view=view)


# ---------- Tournament submission panel ----------
TOURNAMENT_PANEL_TITLE = "Tournament Submissions"
TOURNAMENT_COMPETITOR_EMOJI = "<:SilverTrophy:1528216893297791098>"
TOURNAMENT_SUB_EMOJI = "<:Revolver:1528216974973210747>"

# Matches a slot line like "`1.` " (empty) or "`1.` <@123456789012345678>" (filled)
_TOURNAMENT_SLOT_LINE_RE = re.compile(r"^`(\d+)\.`\s*(?:<@!?(\d+)>)?\s*$")


def build_tournament_submission_content(
    competitor_count: int, sub_count: int, competitors: list = None, subs: list = None
) -> str:
    """Builds the '**Tournament Submission**' message body. `competitors`/`subs` are lists of
    user IDs (or None for an empty slot); if omitted, all slots start empty."""
    competitors = list(competitors) if competitors is not None else [None] * competitor_count
    subs = list(subs) if subs is not None else [None] * sub_count

    lines = ["**Tournament Submission**", f"{TOURNAMENT_COMPETITOR_EMOJI} Competitors :"]
    for i in range(competitor_count):
        filler = f"<@{competitors[i]}>" if i < len(competitors) and competitors[i] else ""
        lines.append(f"`{i + 1}.` {filler}".rstrip())

    lines.append(f"{TOURNAMENT_SUB_EMOJI}  Subs :")
    for i in range(sub_count):
        filler = f"<@{subs[i]}>" if i < len(subs) and subs[i] else ""
        lines.append(f"`{i + 1}.` {filler}".rstrip())

    return "\n".join(lines)


def parse_tournament_submission_content(content: str):
    """Reads a tournament submission message back into (competitor_ids, sub_ids) lists,
    where each entry is a user ID or None for an empty slot."""
    competitors, subs = [], []
    section = None
    for line in content.split("\n"):
        if line.startswith(TOURNAMENT_COMPETITOR_EMOJI):
            section = "competitors"
            continue
        if line.startswith(TOURNAMENT_SUB_EMOJI):
            section = "subs"
            continue
        match = _TOURNAMENT_SLOT_LINE_RE.match(line)
        if not match:
            continue
        user_id = int(match.group(2)) if match.group(2) else None
        if section == "competitors":
            competitors.append(user_id)
        elif section == "subs":
            subs.append(user_id)
    return competitors, subs


class TournamentSubmissionView(discord.ui.View):
    """Attached to each '**Tournament Submission**' message. Reads/writes its state straight
    from the message content, so it works for any number of these messages with one
    persistent, restart-proof view."""

    def __init__(self):
        super().__init__(timeout=None)

    async def _update_signup(self, interaction: discord.Interaction, target: str):
        message = interaction.message
        competitors, subs = parse_tournament_submission_content(message.content)
        user_id = interaction.user.id
        in_competitors = user_id in competitors
        in_subs = user_id in subs

        if target == "remove":
            if not in_competitors and not in_subs:
                await interaction.response.send_message(
                    "You're not currently signed up on this sheet.", ephemeral=True
                )
                return
            if in_competitors:
                competitors[competitors.index(user_id)] = None
            if in_subs:
                subs[subs.index(user_id)] = None
        else:
            target_list = competitors if target == "competitors" else subs
            label = "competitor" if target == "competitors" else "sub"

            if user_id in target_list:
                await interaction.response.send_message(
                    f"You're already signed up as a {label}.", ephemeral=True
                )
                return
            if None not in target_list:
                await interaction.response.send_message(
                    f"There are no open {label} slots.", ephemeral=True
                )
                return

            # moving from the other list, if they were on it
            if in_competitors:
                competitors[competitors.index(user_id)] = None
            if in_subs:
                subs[subs.index(user_id)] = None

            target_list[target_list.index(None)] = user_id

        new_content = build_tournament_submission_content(len(competitors), len(subs), competitors, subs)
        await interaction.response.edit_message(content=new_content)

    @discord.ui.button(
        label="Competitors", style=discord.ButtonStyle.primary, custom_id="tournament_submission_competitors"
    )
    async def competitors_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_signup(interaction, "competitors")

    @discord.ui.button(
        label="Subs", style=discord.ButtonStyle.primary, custom_id="tournament_submission_subs"
    )
    async def subs_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_signup(interaction, "subs")

    @discord.ui.button(
        label="Remove", style=discord.ButtonStyle.danger, custom_id="tournament_submission_remove"
    )
    async def remove_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._update_signup(interaction, "remove")

    @discord.ui.button(
        label="Submit", style=discord.ButtonStyle.success, custom_id="tournament_submission_submit"
    )
    async def submit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        team_key = find_team_by_channel(db["teams"], interaction.channel_id)
        if not team_key:
            await interaction.response.send_message(
                "Couldn't figure out which team this submission sheet belongs to.", ephemeral=True
            )
            return

        info = db["teams"][team_key]
        if interaction.user.id != info.get("leader_id"):
            await interaction.response.send_message(
                "Only the team leader can submit this.", ephemeral=True
            )
            return

        competitors, subs = parse_tournament_submission_content(interaction.message.content)
        recipient_ids = [uid for uid in (competitors + subs) if uid is not None]
        if not recipient_ids:
            await interaction.response.send_message(
                "Nobody has signed up yet — nothing to submit.", ephemeral=True
            )
            return

        panel_channel = bot.get_channel(TOURNAMENT_PANEL_CHANNEL_ID) or await bot.fetch_channel(
            TOURNAMENT_PANEL_CHANNEL_ID
        )

        content = (
            f"**{team_key}** submission\n\n"
            + build_tournament_submission_content(len(competitors), len(subs), competitors, subs)
        )
        recipient_view = await build_code_recipient_view(interaction.guild, recipient_ids)
        await panel_channel.send(content=content, view=recipient_view)

        await interaction.response.send_message(
            f"Submitted — posted in {panel_channel.mention}.", ephemeral=True
        )


class TournamentSubmissionModal(discord.ui.Modal):
    def __init__(self, team_name: str):
        super().__init__(title=f"Tournament Submission — {team_name}"[:45])
        self.team_name = team_name
        self.competitors_input = discord.ui.TextInput(
            label="How much competitors?", placeholder="e.g. 5", max_length=3
        )
        self.backups_input = discord.ui.TextInput(
            label="How much backups?", placeholder="e.g. 2", max_length=3
        )
        self.add_item(self.competitors_input)
        self.add_item(self.backups_input)

    async def on_submit(self, interaction: discord.Interaction):
        competitors_raw = self.competitors_input.value.strip()
        backups_raw = self.backups_input.value.strip()

        if not competitors_raw.isdigit() or not backups_raw.isdigit():
            await interaction.response.send_message(
                "Both fields need to be whole numbers.", ephemeral=True
            )
            return

        competitor_count = int(competitors_raw)
        backup_count = int(backups_raw)

        if not (1 <= competitor_count <= 50) or not (0 <= backup_count <= 50):
            await interaction.response.send_message(
                "Use a competitor count between 1–50 and a backup count between 0–50.", ephemeral=True
            )
            return

        db = load_db()
        info = db["teams"].get(self.team_name)
        if info is None:
            await interaction.response.send_message(
                "That team no longer exists — the panel may be out of date.", ephemeral=True
            )
            return

        channel = interaction.guild.get_channel(info["channel_id"])
        if channel is None:
            await interaction.response.send_message(
                "That team's channel no longer exists.", ephemeral=True
            )
            return

        content = build_tournament_submission_content(competitor_count, backup_count)
        await channel.send(content=content, view=TournamentSubmissionView())

        await interaction.response.send_message(
            f"Tournament submission sheet posted in {channel.mention}.", ephemeral=True
        )


TOURNAMENT_CODE_RECIPIENTS_PER_PAGE = 25
_TOURNAMENT_CODE_PAGE_RE = re.compile(r"page (\d+)/(\d+)")
_MENTION_RE = re.compile(r"<@!?(\d+)>")


def extract_mentions_in_order(content: str) -> list:
    """Pulls every user-mention ID out of a message's content, in order, deduplicated."""
    seen = set()
    ids = []
    for match in _MENTION_RE.finditer(content):
        user_id = int(match.group(1))
        if user_id not in seen:
            seen.add(user_id)
            ids.append(user_id)
    return ids


class CodeModal(discord.ui.Modal):
    def __init__(self, recipient_id: int):
        super().__init__(title="Send Code")
        self.recipient_id = recipient_id
        self.code_input = discord.ui.TextInput(
            label="What's the code?", placeholder="e.g. ABCD-1234", max_length=200
        )
        self.add_item(self.code_input)

    async def on_submit(self, interaction: discord.Interaction):
        code = self.code_input.value.strip()
        if not code:
            await interaction.response.send_message("The code can't be empty.", ephemeral=True)
            return

        guild = interaction.guild
        member = guild.get_member(self.recipient_id)
        if member is None:
            try:
                member = await guild.fetch_member(self.recipient_id)
            except discord.HTTPException:
                member = None

        if member is None:
            await interaction.response.send_message(
                "Couldn't find that member in the server anymore.", ephemeral=True
            )
            return

        try:
            await member.send(f"Your tournament code: `{code}`")
        except discord.Forbidden:
            await interaction.response.send_message(
                f"Couldn't DM {member.mention} — they may have DMs off.", ephemeral=True
            )
            return

        await interaction.response.send_message(f"Code sent to {member.mention} ✅", ephemeral=True)


class CodeRecipientSelectView(discord.ui.View):
    """Posted alongside a submitted team's sheet in the panel channel. Lets a staff member
    pick a person and DM them a code. Like the team-select panel, current page is read
    back from the message's own select placeholder rather than stored on the view, and the
    full recipient list is re-derived from the message's mentions — so it survives restarts."""

    def __init__(self, options: list, page: int = 0, total_pages: int = 1, keep_nav_buttons: bool = False):
        super().__init__(timeout=None)
        if not options:
            options = [discord.SelectOption(label="No one signed up", value="__none__")]
        self.recipient_select.options = options[:25]

        placeholder = "Select a person..."
        if total_pages > 1:
            placeholder += f" (page {page + 1}/{total_pages})"
        self.recipient_select.placeholder = placeholder

        if total_pages <= 1 and not keep_nav_buttons:
            self.remove_item(self.prev_page)
            self.remove_item(self.next_page)
        else:
            self.prev_page.disabled = page <= 0
            self.next_page.disabled = page >= total_pages - 1

    @discord.ui.select(
        placeholder="Select a person...",
        custom_id="tournament_code_recipient_select",
        options=[discord.SelectOption(label="placeholder", value="placeholder")],
    )
    async def recipient_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        value = select.values[0]
        if value == "__none__":
            await interaction.response.send_message("There's no one to send a code to.", ephemeral=True)
            return
        await interaction.response.send_modal(CodeModal(int(value)))

    @discord.ui.button(
        label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="tournament_code_prev_page", row=1
    )
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go_to_page(interaction, -1)

    @discord.ui.button(
        label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="tournament_code_next_page", row=1
    )
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go_to_page(interaction, 1)

    async def _go_to_page(self, interaction: discord.Interaction, delta: int):
        current_page = 0
        for row in interaction.message.components:
            for component in row.children:
                if getattr(component, "custom_id", None) == "tournament_code_recipient_select":
                    match = _TOURNAMENT_CODE_PAGE_RE.search(component.placeholder or "")
                    if match:
                        current_page = int(match.group(1)) - 1

        recipient_ids = extract_mentions_in_order(interaction.message.content)
        new_view = await build_code_recipient_view(interaction.guild, recipient_ids, page=current_page + delta)
        await interaction.response.edit_message(view=new_view)


async def build_code_recipient_view(guild: discord.Guild, recipient_ids: list, page: int = 0) -> CodeRecipientSelectView:
    total_pages = (
        max(1, -(-len(recipient_ids) // TOURNAMENT_CODE_RECIPIENTS_PER_PAGE)) if recipient_ids else 1
    )
    page = max(0, min(page, total_pages - 1))
    start = page * TOURNAMENT_CODE_RECIPIENTS_PER_PAGE
    page_ids = recipient_ids[start:start + TOURNAMENT_CODE_RECIPIENTS_PER_PAGE]

    options = []
    for user_id in page_ids:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.HTTPException:
                member = None
        label = member.display_name if member else f"Unknown user ({user_id})"
        options.append(discord.SelectOption(label=label[:100], value=str(user_id)))

    return CodeRecipientSelectView(options, page=page, total_pages=total_pages)


TOURNAMENT_TEAMS_PER_PAGE = 25
_TOURNAMENT_PAGE_RE = re.compile(r"page (\d+)/(\d+)")


class TournamentTeamSelectView(discord.ui.View):
    """The dropdown panel itself. Discord caps select menus at 25 options, so teams are
    split across pages of 25 with Prev/Next buttons once there are more than that.

    Current page isn't kept on the view instance — it's read back from the live message's
    select placeholder (e.g. "... (page 2/3)") whenever Prev/Next is pressed, since a
    persistent view's registered instance is shared across every message using it and
    can't hold per-message state that survives a restart."""

    def __init__(self, team_names: list = None, page: int = 0, keep_nav_buttons: bool = False):
        super().__init__(timeout=None)
        all_names = list(team_names or [])
        total_pages = max(1, -(-len(all_names) // TOURNAMENT_TEAMS_PER_PAGE)) if all_names else 1
        page = max(0, min(page, total_pages - 1))
        start = page * TOURNAMENT_TEAMS_PER_PAGE
        page_names = all_names[start:start + TOURNAMENT_TEAMS_PER_PAGE]

        options = [discord.SelectOption(label=name[:100], value=name[:100]) for name in page_names]
        if not options:
            options = [discord.SelectOption(label="No teams yet", value="__none__")]
        self.team_select.options = options

        placeholder = "Select a team..."
        if total_pages > 1:
            placeholder += f" (page {page + 1}/{total_pages})"
        self.team_select.placeholder = placeholder

        if total_pages <= 1 and not keep_nav_buttons:
            self.remove_item(self.prev_page)
            self.remove_item(self.next_page)
        else:
            self.prev_page.disabled = page <= 0
            self.next_page.disabled = page >= total_pages - 1

    @discord.ui.select(
        placeholder="Select a team...",
        custom_id="tournament_team_select",
        options=[discord.SelectOption(label="placeholder", value="placeholder")],
    )
    async def team_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        team_name = select.values[0]
        if team_name == "__none__":
            await interaction.response.send_message("There are no teams yet.", ephemeral=True)
            return

        db = load_db()
        if team_name not in db["teams"]:
            await interaction.response.send_message(
                "That team no longer exists — the panel may be out of date.", ephemeral=True
            )
            return

        await interaction.response.send_modal(TournamentSubmissionModal(team_name))

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary, custom_id="tournament_team_prev_page", row=1)
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go_to_page(interaction, -1)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary, custom_id="tournament_team_next_page", row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go_to_page(interaction, 1)

    async def _go_to_page(self, interaction: discord.Interaction, delta: int):
        current_page = 0
        for row in interaction.message.components:
            for component in row.children:
                if getattr(component, "custom_id", None) == "tournament_team_select":
                    match = _TOURNAMENT_PAGE_RE.search(component.placeholder or "")
                    if match:
                        current_page = int(match.group(1)) - 1

        db = load_db()
        team_names = sorted(db["teams"].keys())
        new_view = TournamentTeamSelectView(team_names, page=current_page + delta)
        await interaction.response.edit_message(view=new_view)


async def refresh_tournament_panel():
    """Deletes any previously posted tournament panel in the target channel and posts a
    fresh one listing the current teams. Called on every bot startup so the panel never
    goes stale or duplicates across restarts."""
    channel = bot.get_channel(TOURNAMENT_PANEL_CHANNEL_ID) or await bot.fetch_channel(TOURNAMENT_PANEL_CHANNEL_ID)

    async for msg in channel.history(limit=50):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == TOURNAMENT_PANEL_TITLE:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    db = load_db()
    team_names = sorted(db["teams"].keys())

    embed = discord.Embed(
        title=TOURNAMENT_PANEL_TITLE,
        description=(
            "Select your team below to submit your competitors and backups for the tournament.\n"
            "Use Prev/Next to page through teams if there are more than 25."
        ),
        colour=discord.Colour.gold(),
    )
    await channel.send(embed=embed, view=TournamentTeamSelectView(team_names))


# ============================================================
# BRACKET — Interactive panel, image generation, and commands
# ============================================================

BRACKET_PANEL_TITLE = "Tournament Bracket"


def build_bracket_panel_embed(bracket: BracketState = None) -> discord.Embed:
    """Build the bracket control panel embed."""
    embed = discord.Embed(
        title="🏆 " + BRACKET_PANEL_TITLE,
        description=(
            "Use the buttons below to manage the tournament bracket.\n"
            "**Add** teams → **Generate** the bracket → **Pick winners** → Champion!"
        ),
        colour=discord.Colour.gold(),
    )
    if bracket:
        slots_filled = sum(1 for p in bracket.participants if p != "N/A")
        embed.add_field(
            name="Status",
            value=f"**{slots_filled}/8** teams registered",
            inline=True,
        )
        playable = bracket.playable_matches()
        if playable:
            embed.add_field(
                name="Ready",
                value=f"**{len(playable)}** match(es) to resolve",
                inline=True,
            )
    return embed


async def _refresh_bracket_panel(guild: discord.Guild, bracket: BracketState = None):
    """Delete old bracket panels in the panel channel and post a fresh one."""
    channel = guild.get_channel(BRACKET_PANEL_CHANNEL_ID) or await guild.fetch_channel(BRACKET_PANEL_CHANNEL_ID)

    async for msg in channel.history(limit=30):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == "🏆 " + BRACKET_PANEL_TITLE:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass

    embed = build_bracket_panel_embed(bracket)
    view = BracketPanelView(bracket)
    await channel.send(embed=embed, view=view)


async def _refresh_bracket_public(guild: discord.Guild, bracket: BracketState):
    """Post or edit the bracket image in the public channel."""
    channel = guild.get_channel(BRACKET_PUBLIC_CHANNEL_ID) or await guild.fetch_channel(BRACKET_PUBLIC_CHANNEL_ID)

    # Generate image
    generate_bracket_image(bracket, BRACKET_IMAGE_PATH)
    file = discord.File(BRACKET_IMAGE_PATH, filename=BRACKET_IMAGE_FILENAME)

    embed = discord.Embed(
        title="🏆 " + bracket.title,
        colour=discord.Colour.gold(),
    )
    embed.set_image(url=f"attachment://{BRACKET_IMAGE_FILENAME}")

    # Find and edit existing bracket message, or send a new one
    async for msg in channel.history(limit=30):
        if msg.author.id == bot.user.id and msg.embeds and msg.embeds[0].title == "🏆 " + bracket.title:
            try:
                await msg.edit(embed=embed, attachments=[file])
                return
            except discord.HTTPException:
                pass

    await channel.send(embed=embed, file=file)


# --- Bracket views (persistent) ---

class BracketPanelView(discord.ui.View):
    """Persistent view for the bracket control panel."""
    def __init__(self, bracket: BracketState = None):
        super().__init__(timeout=None)
        self._populate_winner_select(bracket)

    def _populate_winner_select(self, bracket: BracketState = None):
        """Fill the winner-select dropdown with the bracket's currently playable matches
        (both teams known, no winner picked yet). Without this the select only ever showed
        a hardcoded placeholder and could never actually be used to pick a winner."""
        options = []
        if bracket is not None:
            for r, m in bracket.playable_matches():
                team_a = bracket._team_in_match(r, m, 0)
                team_b = bracket._team_in_match(r, m, 1)
                label = f"{bracket.match_label(r, m)}: {team_a} vs {team_b}"
                options.append(discord.SelectOption(label=label[:100], value=f"r{r}m{m}"))

        if options:
            self.winner_select.options = options[:25]
            self.winner_select.placeholder = "Select a match to pick a winner…"
            self.winner_select.disabled = False
        else:
            self.winner_select.options = [
                discord.SelectOption(label="No matches ready yet", value="__placeholder__")
            ]
            self.winner_select.placeholder = "No matches ready — add teams first"
            self.winner_select.disabled = True

    @discord.ui.button(
        label="Add Participant", style=discord.ButtonStyle.success,
        custom_id="bracket_add_participant", row=0,
    )
    async def add_participant(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.response.send_message("No bracket exists. Create one first.", ephemeral=True)
            return

        if "N/A" not in bracket.participants:
            await interaction.response.send_message("All 8 slots are full.", ephemeral=True)
            return

        # Offer a dropdown of registered teams that aren't already in the bracket, rather
        # than free-text entry — keeps bracket entries tied to real teams and avoids typos.
        already_in = {p.lower() for p in bracket.participants if p != "N/A"}
        available_teams = sorted(
            name for name in db["teams"].keys() if name.lower() not in already_in
        )

        if not available_teams:
            await interaction.response.send_message(
                "Every registered team is already in the bracket (or there are no teams yet).",
                ephemeral=True,
            )
            return

        view = BracketAddSelectView(available_teams)
        await interaction.response.send_message(
            "Select a team to add to the bracket:", view=view, ephemeral=True,
        )

    @discord.ui.button(
        label="Remove Participant", style=discord.ButtonStyle.danger,
        custom_id="bracket_remove_participant", row=0,
    )
    async def remove_participant(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.response.send_message("No bracket exists. Create one first.", ephemeral=True)
            return

        filled = [(i, p) for i, p in enumerate(bracket.participants) if p != "N/A"]
        if not filled:
            await interaction.response.send_message("No participants to remove.", ephemeral=True)
            return

        view = BracketRemoveSelectView(filled)
        await interaction.response.send_message(
            "Select a participant to remove:", view=view, ephemeral=True,
        )

    @discord.ui.button(
        label="Generate / Update", style=discord.ButtonStyle.primary,
        custom_id="bracket_generate", row=1,
    )
    async def generate_bracket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.followup.send("No bracket exists. Create one first.", ephemeral=True)
            return

        await _refresh_bracket_public(interaction.guild, bracket)
        await _refresh_bracket_panel(interaction.guild, bracket)
        await interaction.followup.send("Bracket updated — match list refreshed.", ephemeral=True)

    @discord.ui.button(
        label="Randomize Seeds", style=discord.ButtonStyle.secondary,
        emoji="🎲", custom_id="bracket_randomize", row=1,
    )
    async def randomize_seeds(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.followup.send("No bracket exists.", ephemeral=True)
            return

        any_winner_set = any(
            match.get("winner") is not None for rnd in bracket.matches for match in rnd
        )
        if any_winner_set:
            await interaction.followup.send(
                "Can't reshuffle seeds once matches have been played — clear the bracket first "
                "if you want a fresh draw.",
                ephemeral=True,
            )
            return

        filled = [p for p in bracket.participants if p != "N/A"]
        if len(filled) < 2:
            await interaction.followup.send("Add at least 2 teams before randomizing.", ephemeral=True)
            return

        import random
        random.shuffle(filled)
        bracket.participants = filled + ["N/A"] * (BRACKET_TOTAL_SLOTS - len(filled))
        bracket.history = []
        save_bracket(db, bracket)
        save_db(db)

        generate_bracket_image(bracket, BRACKET_IMAGE_PATH)
        await _refresh_bracket_public(interaction.guild, bracket)
        await _refresh_bracket_panel(interaction.guild, bracket)
        await interaction.followup.send("🎲 Seeds shuffled — the draw has been re-rolled.", ephemeral=True)

    @discord.ui.button(
        label="Undo Last Result", style=discord.ButtonStyle.secondary,
        emoji="↩️", custom_id="bracket_undo", row=1,
    )
    async def undo_last(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.followup.send("No bracket exists.", ephemeral=True)
            return

        undone = bracket.undo_last()
        if undone is None:
            await interaction.followup.send("Nothing to undo yet.", ephemeral=True)
            return

        save_bracket(db, bracket)
        save_db(db)

        generate_bracket_image(bracket, BRACKET_IMAGE_PATH)
        await _refresh_bracket_public(interaction.guild, bracket)
        await _refresh_bracket_panel(interaction.guild, bracket)

        r, m = undone
        await interaction.followup.send(
            f"↩️ Undid the result of **{bracket.match_label(r, m)}**.", ephemeral=True
        )

    @discord.ui.button(
        label="Clear Bracket", style=discord.ButtonStyle.danger,
        custom_id="bracket_clear", row=2,
    )
    async def clear_bracket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.followup.send("No bracket to clear.", ephemeral=True)
            return

        bracket.clear_all()
        save_bracket(db, bracket)
        save_db(db)

        generate_bracket_image(bracket, BRACKET_IMAGE_PATH)
        await _refresh_bracket_public(interaction.guild, bracket)
        await _refresh_bracket_panel(interaction.guild, bracket)
        await interaction.followup.send("Bracket cleared.", ephemeral=True)

    @discord.ui.select(
        placeholder="Select a match to pick a winner…",
        custom_id="bracket_winner_select",
        options=[discord.SelectOption(label="Refresh to see matches", value="__placeholder__")],
    )
    async def winner_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        value = select.values[0]
        if value == "__placeholder__":
            await interaction.response.send_message(
                "No matches are ready yet — add participants until both slots of a match are filled.",
                ephemeral=True,
            )
            return

        # Parse "r{round}m{match}"
        try:
            r = int(value[1:value.index("m")])
            m = int(value[value.index("m") + 1:])
        except (ValueError, IndexError):
            await interaction.response.send_message("Invalid match reference.", ephemeral=True)
            return

        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.response.send_message("No bracket exists.", ephemeral=True)
            return

        team_a = bracket._team_in_match(r, m, 0)
        team_b = bracket._team_in_match(r, m, 1)

        if team_a in ("N/A", "TBD") or team_b in ("N/A", "TBD"):
            await interaction.response.send_message("This match isn't ready yet.", ephemeral=True)
            return

        view = BracketWinnerConfirmView(r, m, team_a, team_b)
        await interaction.response.send_message(
            f"**{bracket.match_label(r, m)}**\nPick the winner:", view=view, ephemeral=True,
        )


class BracketAddSelectView(discord.ui.View):
    """Ephemeral dropdown listing registered teams that aren't in the bracket yet.
    Discord caps selects at 25 options; if there are more teams than that, only the
    first 25 (alphabetically) are shown — same convention as the other pickers here."""

    def __init__(self, available_teams: list):
        super().__init__(timeout=60)
        options = [discord.SelectOption(label=name[:100], value=name) for name in available_teams[:25]]
        self.add_select.options = options

    @discord.ui.select(
        placeholder="Select a team…",
        custom_id="bracket_add_select",
        options=[discord.SelectOption(label="placeholder", value="placeholder")],
    )
    async def add_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        team_name = select.values[0]

        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.response.send_message("No bracket exists.", ephemeral=True)
            return

        if team_name.lower() in [p.lower() for p in bracket.participants if p != "N/A"]:
            await interaction.response.send_message(f"**{team_name}** is already in the bracket.", ephemeral=True)
            return

        slot = None
        for i, p in enumerate(bracket.participants):
            if p == "N/A":
                slot = i
                break

        if slot is None:
            await interaction.response.send_message("All 8 slots are full.", ephemeral=True)
            return

        playable_before = set(bracket.playable_matches())

        bracket.participants[slot] = team_name
        save_bracket(db, bracket)
        save_db(db)

        await interaction.response.defer(ephemeral=True)

        await _refresh_bracket_panel(interaction.guild, bracket)
        generate_bracket_image(bracket, BRACKET_IMAGE_PATH)
        await _refresh_bracket_public(interaction.guild, bracket)

        newly_ready = [rm for rm in bracket.playable_matches() if rm not in playable_before]
        if newly_ready:
            await _announce_ready_matches(interaction.guild, db, bracket, newly_ready)

        file = discord.File(BRACKET_IMAGE_PATH, filename=BRACKET_IMAGE_FILENAME)
        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(
            content=f"✅ Added **{team_name}** to slot {slot + 1}.", view=self, attachments=[file],
        )


class BracketRemoveSelectView(discord.ui.View):
    """Ephemeral dropdown listing filled slots for removal."""
    def __init__(self, filled: list[tuple[int, str]]):
        super().__init__(timeout=60)
        options = [
            discord.SelectOption(label=name[:100], value=str(idx), description=f"Slot {idx + 1}")
            for idx, name in filled
        ]
        self.remove_select.options = options[:25]

    @discord.ui.select(
        placeholder="Select a team to remove…",
        custom_id="bracket_remove_select",
        options=[discord.SelectOption(label="placeholder", value="placeholder")],
    )
    async def remove_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        slot = int(select.values[0])

        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.response.send_message("No bracket exists.", ephemeral=True)
            return

        removed_name = bracket.participants[slot]
        bracket.participants[slot] = "N/A"
        bracket.clear_after(slot)
        save_bracket(db, bracket)
        save_db(db)

        await _refresh_bracket_panel(interaction.guild, bracket)
        generate_bracket_image(bracket, BRACKET_IMAGE_PATH)
        await _refresh_bracket_public(interaction.guild, bracket)
        await interaction.response.send_message(f"Removed **{removed_name}** from slot {slot + 1}.", ephemeral=True)


class BracketWinnerConfirmView(discord.ui.View):
    """Ephemeral confirm for picking a winner between two teams."""
    def __init__(self, r: int, m: int, team_a: str, team_b: str):
        super().__init__(timeout=60)
        self.r = r
        self.m = m
        self.team_a = team_a
        self.team_b = team_b

    @discord.ui.button(label="A", style=discord.ButtonStyle.success, custom_id="bracket_winner_a")
    async def pick_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, 0, self.team_a)

    @discord.ui.button(label="B", style=discord.ButtonStyle.success, custom_id="bracket_winner_b")
    async def pick_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick(interaction, 1, self.team_b)

    async def _pick(self, interaction: discord.Interaction, slot: int, winner_name: str):
        await interaction.response.defer(ephemeral=True)

        db = load_db()
        bracket = load_bracket(db)
        if not bracket:
            await interaction.followup.send("No bracket exists.", ephemeral=True)
            return

        playable_before = set(bracket.playable_matches())

        if not bracket.set_winner(self.r, self.m, slot):
            await interaction.followup.send("Can't set that winner — match may no longer be playable.", ephemeral=True)
            return

        save_bracket(db, bracket)
        save_db(db)

        generate_bracket_image(bracket, BRACKET_IMAGE_PATH)
        await _refresh_bracket_public(interaction.guild, bracket)
        await _refresh_bracket_panel(interaction.guild, bracket)

        # Check for champion
        final_match = bracket.matches[2][0]
        champion = None
        if final_match.get("winner") is not None:
            champion = bracket.participants[final_match["winner"]]

        msg = f"✅ **{winner_name}** advances!"
        if champion:
            msg += f"\n\n🏆 **Champion: {champion}!**"

        for child in self.children:
            child.disabled = True
        await interaction.followup.send(msg, ephemeral=True)

        if champion:
            await _announce_champion(interaction.guild, db, bracket, champion)
        else:
            newly_ready = [rm for rm in bracket.playable_matches() if rm not in playable_before]
            if newly_ready:
                await _announce_ready_matches(interaction.guild, db, bracket, newly_ready)


async def _team_leader_mention(guild: discord.Guild, db: dict, team_name: str) -> str:
    """Best-effort mention for a team's leader, falling back to the plain team name if the
    team isn't in the database or the leader can no longer be resolved."""
    info = db["teams"].get(team_name)
    if not info:
        return f"**{team_name}**"
    leader_id = info.get("leader_id")
    if leader_id is None:
        return f"**{team_name}**"
    member = guild.get_member(leader_id)
    if member is None:
        try:
            member = await guild.fetch_member(leader_id)
        except discord.HTTPException:
            return f"**{team_name}**"
    return f"{member.mention} (**{team_name}**)"


async def _announce_ready_matches(guild: discord.Guild, db: dict, bracket: BracketState, matches: list):
    """Pings both teams' leaders in the public bracket channel whenever a match becomes
    playable (both sides now known), so nobody has to keep refreshing the bracket image
    to find out who's up next."""
    channel = guild.get_channel(BRACKET_PUBLIC_CHANNEL_ID) or await guild.fetch_channel(BRACKET_PUBLIC_CHANNEL_ID)
    for r, m in matches:
        team_a = bracket._team_in_match(r, m, 0)
        team_b = bracket._team_in_match(r, m, 1)
        mention_a = await _team_leader_mention(guild, db, team_a)
        mention_b = await _team_leader_mention(guild, db, team_b)
        try:
            await channel.send(
                f"⚔️ **{bracket.match_label(r, m)} is ready!** {mention_a} vs {mention_b} — good luck!"
            )
        except discord.HTTPException:
            pass


async def _announce_champion(guild: discord.Guild, db: dict, bracket: BracketState, champion: str):
    """Posts a celebratory embed once the final has a winner."""
    channel = guild.get_channel(BRACKET_PUBLIC_CHANNEL_ID) or await guild.fetch_channel(BRACKET_PUBLIC_CHANNEL_ID)
    mention = await _team_leader_mention(guild, db, champion)

    embed = discord.Embed(
        title="🏆🎉 WE HAVE A CHAMPION! 🎉🏆",
        description=f"# {champion}\n\nCongratulations {mention} — undefeated champions of **{bracket.title}**!",
        colour=discord.Colour.gold(),
    )
    if os.path.exists(BRACKET_IMAGE_PATH):
        embed.set_image(url=f"attachment://{BRACKET_IMAGE_FILENAME}")
        file = discord.File(BRACKET_IMAGE_PATH, filename=BRACKET_IMAGE_FILENAME)
        await channel.send(content="🎊🎊🎊", embed=embed, file=file)
    else:
        await channel.send(content="🎊🎊🎊", embed=embed)


# --- Bracket sync on startup ---

async def sync_bracket(guild: discord.Guild):
    """On startup: refresh both bracket channels if a bracket exists."""
    db = load_db()
    bracket = load_bracket(db)
    if bracket is None:
        return

    try:
        generate_bracket_image(bracket, BRACKET_IMAGE_PATH)
        await _refresh_bracket_public(guild, bracket)
    except discord.HTTPException as e:
        print(f"Failed to refresh bracket public channel: {e}")

    try:
        await _refresh_bracket_panel(guild, bracket)
    except discord.HTTPException as e:
        print(f"Failed to refresh bracket panel: {e}")


async def perform_team_deletion(db: dict, team_name: str, guild: discord.Guild, reason: str) -> bool:
    """Removes a team's role, channel, and DB entry. Returns False if the team was already gone."""
    info = db["teams"].pop(team_name, None)
    if info is None:
        return False

    role = guild.get_role(info["role_id"])
    if role:
        await role.delete(reason=reason)

    channel = guild.get_channel(info["channel_id"])
    if channel:
        await channel.delete(reason=reason)

    leader_id = info.get("leader_id")
    if leader_id is not None:
        leader_marker_role = guild.get_role(TEAM_LEADER_ROLE_ID)
        if leader_marker_role is not None:
            try:
                leader_member = guild.get_member(leader_id) or await guild.fetch_member(leader_id)
                if leader_marker_role in leader_member.roles:
                    await leader_member.remove_roles(leader_marker_role, reason=reason)
            except discord.HTTPException:
                pass

    save_db(db)
    await backup_db_to_log_channel()
    return True


async def sync_existing_teams():
    """Backfill pass run on every startup: makes sure every current team leader holds
    TEAM_LEADER_ROLE_ID and has the manage-messages/mention-everyone overrides in their
    own team channel (so they can ping the team, delete messages, and pin messages).
    Idempotent — cheap after the first run, and self-heals if a permission or role is
    ever reverted manually."""
    db = load_db()
    if not db["teams"]:
        return

    guild = None
    leader_role_granted = 0
    perms_updated = 0

    for team_name, info in db["teams"].items():
        leader_id = info.get("leader_id")
        channel_id = info.get("channel_id")
        if leader_id is None:
            continue

        try:
            if guild is None:
                # all teams live in one guild for this bot; grab it from any known channel
                seed_channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
                guild = seed_channel.guild
            member = guild.get_member(leader_id) or await guild.fetch_member(leader_id)
        except discord.HTTPException:
            continue

        leader_marker_role = guild.get_role(TEAM_LEADER_ROLE_ID)
        if leader_marker_role is None:
            print(f"TEAM_LEADER_ROLE_ID ({TEAM_LEADER_ROLE_ID}) not found in guild — skipping role backfill.")
        elif leader_marker_role not in member.roles:
            try:
                await member.add_roles(
                    leader_marker_role, reason=f"Backfilled team-leader role for existing team {team_name}"
                )
                leader_role_granted += 1
            except discord.HTTPException:
                pass

        channel = guild.get_channel(channel_id)
        if channel is not None:
            existing = channel.overwrites_for(member)
            if not (existing.manage_messages and existing.mention_everyone):
                try:
                    await channel.set_permissions(
                        member,
                        overwrite=team_leader_channel_overwrite(),
                        reason=f"Backfilled leader channel permissions for existing team {team_name}",
                    )
                    perms_updated += 1
                except discord.HTTPException:
                    pass

    if leader_role_granted or perms_updated:
        print(
            f"Backfilled team-leader role onto {leader_role_granted} leader(s) and "
            f"channel permissions onto {perms_updated} leader(s)."
        )


# ---------- Delete-existing-team view (shown when a leader tries to make a 2nd team) ----------
class DeleteTeamView(discord.ui.View):
    def __init__(self, author_id: int, team_name: str, guild: discord.Guild):
        super().__init__(timeout=120)
        self.author_id = author_id
        self.team_name = team_name
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Delete current team", style=discord.ButtonStyle.danger)
    async def delete_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        db = load_db()
        deleted = await perform_team_deletion(
            db, self.team_name, self.guild, reason=f"Team deleted by {interaction.user}"
        )
        if not deleted:
            await interaction.edit_original_response(content="That team no longer exists.", view=None)
            return

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(
            content=f"🗑️ Team **{self.team_name}** deleted. You can now create a new one.",
            view=self,
        )


# ---------- Confirmation view for team deletion (used by /changeteamsettings and /staffchangesetting) ----------
class ConfirmDeleteTeamView(discord.ui.View):
    def __init__(self, invoker_id: int, team_name: str, guild: discord.Guild):
        super().__init__(timeout=60)
        self.invoker_id = invoker_id
        self.team_name = team_name
        self.guild = guild

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        db = load_db()
        deleted = await perform_team_deletion(
            db, self.team_name, self.guild, reason=f"Team deleted by staff member {interaction.user}"
        )
        for child in self.children:
            child.disabled = True
        if not deleted:
            await interaction.edit_original_response(content="That team no longer exists.", view=self)
            return
        await interaction.edit_original_response(
            content=f"🗑️ Team **{self.team_name}** has been deleted.", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Cancelled — team not deleted.", view=self)


# ---------- Confirmation view for /cleanuporphanteams ----------
class ConfirmCleanupView(discord.ui.View):
    def __init__(self, invoker_id: int, orphans: list):
        super().__init__(timeout=120)
        self.invoker_id = invoker_id
        self.orphans = orphans  # list of (channel, role_or_None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invoker_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes, delete them", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

        deleted_channels = 0
        deleted_roles = 0
        for channel, role in self.orphans:
            if role is not None:
                try:
                    await role.delete(reason=f"Orphan team role cleanup by {interaction.user}")
                    deleted_roles += 1
                except discord.HTTPException:
                    pass
            try:
                await channel.delete(reason=f"Orphan team channel cleanup by {interaction.user}")
                deleted_channels += 1
            except discord.HTTPException:
                pass

        await interaction.followup.send(
            f"🧹 Cleanup complete — deleted {deleted_channels} channel(s) and {deleted_roles} role(s).",
            ephemeral=True,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        await interaction.followup.send("Cleanup cancelled — nothing was deleted.", ephemeral=True)


# ---------- Admin confirmation view for /createteam ----------
class ConfirmTeamView(discord.ui.View):
    def __init__(self, requester_id: int, team_name: str, emoji: str, colour: str, guild: discord.Guild):
        super().__init__(timeout=None)
        self.requester_id = requester_id
        self.team_name = team_name
        self.emoji = emoji
        self.colour = colour
        self.guild = guild
        self.message: discord.Message = None  # set by the caller after sending

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "Only admins can confirm team creation.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

        guild = self.guild
        category = guild.get_channel(TEAM_CATEGORY_ID)

        role_colour = discord.Colour.from_str(self.colour)
        try:
            role = await guild.create_role(
                name=f"{self.team_name} Team",
                colour=role_colour,
                display_icon=self.emoji,
                reason=f"Team created, confirmed by {interaction.user}",
            )
        except discord.HTTPException:
            # Role icons require a certain server boost level; fall back without one
            role = await guild.create_role(
                name=f"{self.team_name} Team",
                colour=role_colour,
                reason=f"Team created, confirmed by {interaction.user} (role icons unavailable)",
            )

        reference_role = guild.get_role(REFERENCE_ROLE_ID)
        if reference_role is not None:
            try:
                await role.edit(
                    position=reference_role.position + 1,
                    reason="Keep team role above reference role",
                )
            except discord.HTTPException:
                # Bot's own top role may be too low to move things this high; skip silently
                pass

        leader = guild.get_member(self.requester_id) or await guild.fetch_member(self.requester_id)

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            role: discord.PermissionOverwrite(view_channel=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True),
            # Leader gets extra rights in their own channel: delete/pin messages, and ping
            # the team role even though it isn't set to be mentionable.
            leader: team_leader_channel_overwrite(),
        }

        channel_name = f"{self.emoji}┃{self.team_name}-Team"
        team_channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"Team created, confirmed by {interaction.user}",
        )

        await leader.add_roles(role, reason="New team leader")

        leader_marker_role = guild.get_role(TEAM_LEADER_ROLE_ID)
        if leader_marker_role is not None:
            try:
                await leader.add_roles(leader_marker_role, reason="New team leader")
            except discord.HTTPException:
                pass

        try:
            await leader.send(f"You're now the leader of **{self.team_name}** {self.emoji}!")
        except discord.Forbidden:
            pass

        db = load_db()
        db["teams"][self.team_name] = {
            "emoji": self.emoji,
            "leader_id": self.requester_id,
            "role_id": role.id,
            "channel_id": team_channel.id,
            "members": [self.requester_id],
        }
        save_db(db)
        await backup_db_to_log_channel()
        pending_team_requests.discard(self.requester_id)

        await interaction.followup.send(
            f"✅ Team **{self.team_name}** {self.emoji} created — {team_channel.mention}"
        )

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
        pending_team_requests.discard(self.requester_id)
        await interaction.followup.send("Team creation denied.", ephemeral=True)


# ---------- Invite response view (DM'd to the invited user) ----------
class InviteResponseView(discord.ui.View):
    def __init__(self, team_name: str, invited_user_id: int, guild_id: int):
        super().__init__(timeout=86400)  # 24h to respond
        self.team_name = team_name
        self.invited_user_id = invited_user_id
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.invited_user_id:
            await interaction.response.send_message("This invite isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()

        db = load_db()
        info = db["teams"].get(self.team_name)
        if info is None:
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(content="This team no longer exists.", view=self)
            return

        if (
            self.invited_user_id not in info.get("members", [])
            and len(info.get("members", [])) >= MAX_TEAM_MEMBERS
        ):
            for child in self.children:
                child.disabled = True
            await interaction.edit_original_response(
                content=f"**{self.team_name}** filled up to the {MAX_TEAM_MEMBERS}-member cap "
                f"before you accepted — ask the leader to check again.",
                view=self,
            )
            return

        guild = bot.get_guild(self.guild_id)
        member = guild.get_member(self.invited_user_id) or await guild.fetch_member(self.invited_user_id)
        role = guild.get_role(info["role_id"])
        if role:
            await member.add_roles(role, reason="Accepted team invite")

        if self.invited_user_id not in info["members"]:
            info["members"].append(self.invited_user_id)
        save_db(db)
        await backup_db_to_log_channel()

        channel = guild.get_channel(info["channel_id"])
        if channel:
            await channel.send(f"🎉 {member.mention} just joined the team!")

        for child in self.children:
            child.disabled = True
        await interaction.edit_original_response(content=f"You joined **{self.team_name}**! 🎉", view=self)

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Invite declined.", view=self)


# ============================================================
# GIVEAWAYS — state, embed, join button, and background ender
# ============================================================

GIVEAWAY_JOIN_EMOJI = "🎉"

# Matches durations like "10m", "2h", "1d", "1d12h", "1w" — one or more (amount, unit) pairs.
_DURATION_RE = re.compile(r"(\d+)\s*(w|d|h|m|s)", re.IGNORECASE)
_DURATION_UNIT_SECONDS = {"w": 604800, "d": 86400, "h": 3600, "m": 60, "s": 1}


def parse_duration(text: str):
    """Parses a duration string like '10m', '2h', or '1d12h' into a timedelta.
    Returns None if nothing valid could be parsed."""
    from datetime import timedelta

    total_seconds = 0
    matched_any = False
    for amount, unit in _DURATION_RE.findall(text.strip()):
        total_seconds += int(amount) * _DURATION_UNIT_SECONDS[unit.lower()]
        matched_any = True

    if not matched_any or total_seconds <= 0:
        return None
    return timedelta(seconds=total_seconds)


def build_giveaway_embed(
    prize: str,
    winners_count: int,
    host_id: int,
    end_ts: int,
    entries_count: int,
    winner_ids: list = None,
) -> discord.Embed:
    """Mirrors the AC: Arena Hub giveaway style: orange embed, a timestamp for when it
    ends (or ended), the host, a live entry count, and — once it's over — the winners."""
    ended = winner_ids is not None

    embed = discord.Embed(
        title=f"{GIVEAWAY_JOIN_EMOJI} {prize}",
        colour=discord.Colour.orange(),
    )

    if ended:
        embed.add_field(name="Ended", value=f"<t:{end_ts}:F>", inline=False)
    else:
        embed.add_field(name="Ends", value=f"<t:{end_ts}:R> (<t:{end_ts}:F>)", inline=False)

    embed.add_field(name="Hosted by", value=f"<@{host_id}>", inline=False)
    embed.add_field(name="Entries", value=f"**{entries_count}**", inline=False)

    if ended:
        winners_value = (
            ", ".join(f"<@{uid}>" for uid in winner_ids) if winner_ids else "No valid entries"
        )
        embed.add_field(name="Winners" if len(winner_ids) != 1 else "Winner", value=winners_value, inline=False)
    else:
        embed.set_footer(
            text=f"{winners_count} winner(s) • Click {GIVEAWAY_JOIN_EMOJI} Join below to enter!"
        )

    embed.set_image(url=f"attachment://{SUPPORT_BANNER_FILENAME}")
    return embed


class GiveawayJoinView(discord.ui.View):
    """Persistent green 'Join' button attached to every giveaway message. Entries are kept
    in the database keyed by message ID (not on the view instance, since one registered
    view instance backs every giveaway message and must survive restarts)."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join", emoji=GIVEAWAY_JOIN_EMOJI, style=discord.ButtonStyle.success,
        custom_id="giveaway_join_button",
    )
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        db = load_db()
        giveaways = db.setdefault("giveaways", {})
        key = str(interaction.message.id)
        info = giveaways.get(key)

        if info is None:
            await interaction.response.send_message("This giveaway no longer exists.", ephemeral=True)
            return
        if info.get("ended"):
            await interaction.response.send_message("This giveaway has already ended.", ephemeral=True)
            return

        entries = info.setdefault("entries", [])
        user_id = interaction.user.id
        if user_id in entries:
            entries.remove(user_id)
            joined = False
        else:
            entries.append(user_id)
            joined = True

        save_db(db)

        embed = build_giveaway_embed(
            prize=info["prize"],
            winners_count=info["winners"],
            host_id=info["host_id"],
            end_ts=info["end_ts"],
            entries_count=len(entries),
        )
        try:
            await interaction.response.edit_message(embed=embed)
        except discord.HTTPException:
            pass

        if joined:
            await interaction.followup.send(f"{GIVEAWAY_JOIN_EMOJI} You're in — good luck!", ephemeral=True)
        else:
            await interaction.followup.send("You left the giveaway.", ephemeral=True)


async def _end_giveaway(guild: discord.Guild, message_id: str, info: dict):
    """Picks winners, edits the giveaway message to its final state, and announces the
    result in the same channel."""
    import random

    entries = info.get("entries", [])
    winners_count = min(info.get("winners", 1), len(entries))
    winner_ids = random.sample(entries, winners_count) if winners_count > 0 else []

    info["ended"] = True
    info["winner_ids"] = winner_ids

    channel = guild.get_channel(info["channel_id"]) or bot.get_channel(info["channel_id"])
    if channel is None:
        try:
            channel = await bot.fetch_channel(info["channel_id"])
        except discord.HTTPException:
            return

    embed = build_giveaway_embed(
        prize=info["prize"],
        winners_count=info["winners"],
        host_id=info["host_id"],
        end_ts=info["end_ts"],
        entries_count=len(entries),
        winner_ids=winner_ids,
    )

    ended_view = GiveawayJoinView()
    for child in ended_view.children:
        child.disabled = True

    try:
        message = await channel.fetch_message(int(message_id))
        await message.edit(embed=embed, view=ended_view)
    except discord.HTTPException:
        pass

    if winner_ids:
        mentions = ", ".join(f"<@{uid}>" for uid in winner_ids)
        try:
            await channel.send(f"{GIVEAWAY_JOIN_EMOJI} Congratulations {mentions} — you won **{info['prize']}**!")
        except discord.HTTPException:
            pass
    else:
        try:
            await channel.send(f"{GIVEAWAY_JOIN_EMOJI} The giveaway for **{info['prize']}** ended with no entries.")
        except discord.HTTPException:
            pass


@tasks.loop(seconds=30)
async def check_giveaways():
    db = load_db()
    giveaways = db.get("giveaways", {})
    if not giveaways:
        return

    now_ts = int(discord.utils.utcnow().timestamp())
    changed = False

    for message_id, info in list(giveaways.items()):
        if info.get("ended") or info.get("end_ts", 0) > now_ts:
            continue

        guild = bot.get_guild(info.get("guild_id")) if info.get("guild_id") else None
        if guild is None:
            # fall back to the first guild the bot can see the channel in
            channel = bot.get_channel(info["channel_id"])
            guild = channel.guild if channel else None
        if guild is None:
            continue

        await _end_giveaway(guild, message_id, info)
        changed = True

    if changed:
        save_db(db)
        await backup_db_to_log_channel()


@check_giveaways.before_loop
async def before_check_giveaways():
    await bot.wait_until_ready()


# ---------- Slash commands ----------
@bot.tree.command(name="createteam", description="Create a new team")
@app_commands.describe(
    name="Team name",
    emoji="A single standard Discord emoji for the team (no custom server emojis)",
    colour="Hex colour for the team's role, e.g. #5865F2",
)
async def createteam(interaction: discord.Interaction, name: str, emoji: str, colour: str):
    await interaction.response.defer(ephemeral=True)

    if not has_create_team_access(interaction.user):
        await interaction.followup.send(
            "You must be level 5 to create a team.", ephemeral=True
        )
        return

    if not is_valid_standard_emoji(emoji):
        await interaction.followup.send(
            "That's not a standard Discord emoji. Please use a single regular emoji "
            "(custom server emojis can't be used in channel names or role icons).",
            ephemeral=True,
        )
        return

    normalized_colour = normalize_hex_colour(colour)
    if normalized_colour is None:
        await interaction.followup.send(
            "That's not a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
        )
        return

    db = load_db()

    if find_team_key_ci(db["teams"], name):
        await interaction.followup.send(
            f"A team called **{name}** already exists. Pick a different name.", ephemeral=True
        )
        return

    existing = find_team_by_leader(db["teams"], interaction.user.id)
    if existing:
        view = DeleteTeamView(interaction.user.id, existing, interaction.guild)
        await interaction.followup.send(
            f"You already lead a team called **{existing}**. You can only lead one team at a time.",
            view=view,
            ephemeral=True,
        )
        return

    existing_membership = find_team_by_member(db["teams"], interaction.user.id)
    if existing_membership:
        await interaction.followup.send(
            f"You're already a member of **{existing_membership}**. Leave that team with "
            f"`/leaveteam` before creating a new one.",
            ephemeral=True,
        )
        return

    if interaction.user.id in pending_team_requests:
        await interaction.followup.send(
            "You already have a team creation request awaiting admin confirmation. "
            "Please wait for that to be approved or denied before submitting another.",
            ephemeral=True,
        )
        return

    pending_team_requests.add(interaction.user.id)

    confirm_channel = bot.get_channel(CONFIRM_CHANNEL_ID) or await bot.fetch_channel(CONFIRM_CHANNEL_ID)
    view = ConfirmTeamView(
        requester_id=interaction.user.id,
        team_name=name,
        emoji=emoji,
        colour=normalized_colour,
        guild=interaction.guild,
    )
    sent = await confirm_channel.send(
        content=f"{interaction.user.mention} wants to create team **{name}** {emoji}. Admins, confirm?",
        view=view,
    )
    view.message = sent
    await interaction.followup.send(
        f"Sent to {confirm_channel.mention} for admin confirmation ✅", ephemeral=True
    )


@bot.tree.command(name="teammembers", description="List a team's members")
@app_commands.describe(team="Team name")
async def teammembers(interaction: discord.Interaction, team: str):
    await interaction.response.defer()

    db = load_db()
    key = find_team_key_ci(db["teams"], team)
    if not key:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    info = db["teams"][key]
    role = interaction.guild.get_role(info["role_id"])
    if role is None:
        await interaction.followup.send("That team's role no longer exists.", ephemeral=True)
        return

    members = sorted(role.members, key=lambda m: m.id != info["leader_id"])
    lines = [
        member.mention + (" (Leader)" if member.id == info["leader_id"] else "")
        for member in members
    ]
    embed = discord.Embed(
        title=f"{info['emoji']} {key} Team",
        description="\n".join(lines) if lines else "No members with this role yet.",
    )
    await interaction.followup.send(embed=embed)


@teammembers.autocomplete("team")
async def teammembers_team_autocomplete(interaction: discord.Interaction, current: str):
    db = load_db()
    return [
        app_commands.Choice(name=key, value=key)
        for key in db["teams"].keys()
        if current.lower() in key.lower()
    ][:25]


@bot.tree.command(name="invite", description="Invite a user to your team")
@app_commands.describe(user="The user to invite")
async def invite(interaction: discord.Interaction, user: discord.Member):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to invite people.", ephemeral=True)
        return

    if user.bot:
        await interaction.followup.send("You can't invite bots.", ephemeral=True)
        return

    if find_team_by_member(db["teams"], user.id):
        await interaction.followup.send("That user is already on a team.", ephemeral=True)
        return

    info = db["teams"][team_key]

    if len(info.get("members", [])) >= MAX_TEAM_MEMBERS:
        await interaction.followup.send(
            f"**{team_key}** is already at the {MAX_TEAM_MEMBERS}-member cap — remove someone first.",
            ephemeral=True,
        )
        return

    view = InviteResponseView(team_key, user.id, interaction.guild.id)
    try:
        await user.send(
            f"{interaction.user.mention} invited you to join **{team_key}** {info['emoji']}! "
            f"Would you like to join?",
            view=view,
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "Couldn't DM that user (they may have DMs off).", ephemeral=True
        )
        return

    await interaction.followup.send(f"Invite sent to {user.mention}.", ephemeral=True)


@bot.tree.command(name="leaveteam", description="Leave your current team")
async def leaveteam(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_member(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You're not in a team.", ephemeral=True)
        return

    info = db["teams"][team_key]
    if interaction.user.id == info["leader_id"]:
        await interaction.followup.send(
            "You're the leader of this team, so you can't leave it. "
            "Use `/changeteamsettings delete:True` if you want to delete it instead.",
            ephemeral=True,
        )
        return

    role = interaction.guild.get_role(info["role_id"])
    if role:
        await interaction.user.remove_roles(role, reason="Left the team")

    info["members"] = [uid for uid in info["members"] if uid != interaction.user.id]
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.followup.send(f"You left **{team_key}**.", ephemeral=True)


@bot.tree.command(name="kickteammember", description="Remove a member from your team")
@app_commands.describe(member="The team member to remove")
async def kickteammember(interaction: discord.Interaction, member: discord.Member):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to use this command.", ephemeral=True)
        return

    info = db["teams"][team_key]

    if member.id == interaction.user.id:
        await interaction.followup.send(
            "You can't kick yourself. Use `/changeteamsettings delete:True` if you want that.",
            ephemeral=True,
        )
        return

    if member.id not in info.get("members", []):
        await interaction.followup.send(f"{member.mention} isn't a member of **{team_key}**.", ephemeral=True)
        return

    role = interaction.guild.get_role(info["role_id"])
    if role:
        await member.remove_roles(role, reason=f"Kicked from team by {interaction.user}")

    info["members"] = [uid for uid in info["members"] if uid != member.id]
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.followup.send(f"Removed {member.mention} from **{team_key}**.", ephemeral=True)


async def team_name_autocomplete(interaction: discord.Interaction, current: str):
    db = load_db()
    return [
        app_commands.Choice(name=key, value=key)
        for key in db["teams"].keys()
        if current.lower() in key.lower()
    ][:25]


@bot.tree.command(
    name="staffchangesetting",
    description="(Staff) Change a team's name, colour, or icon, or delete it",
)
@app_commands.describe(
    team="Team to modify",
    delete="Delete the team — removes the role, channel, and database entry (can't be undone)",
    changename="New team name",
    changecolour="New hex colour for the team's role, e.g. #5865F2",
    changeicon="New single standard emoji for the team (no custom server emojis)",
)
async def staffchangesetting(
    interaction: discord.Interaction,
    team: str,
    delete: bool = False,
    changename: str = None,
    changecolour: str = None,
    changeicon: str = None,
):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    team_key = find_team_key_ci(db["teams"], team)
    if not team_key:
        await interaction.followup.send("No team found with that name.", ephemeral=True)
        return

    if delete:
        view = ConfirmDeleteTeamView(interaction.user.id, team_key, interaction.guild)
        await interaction.followup.send(
            f"Are you sure you want to delete **{team_key}**? This will remove the team's role, "
            f"channel, and database entry, and can't be undone.",
            view=view,
            ephemeral=True,
        )
        return

    if not any([changename, changecolour, changeicon]):
        await interaction.followup.send(
            "You didn't specify anything to change. Provide `changename`, `changecolour`, "
            "`changeicon`, or set `delete:` to True.",
            ephemeral=True,
        )
        return

    if changename and changename.lower() != team_key.lower() and find_team_key_ci(db["teams"], changename):
        await interaction.followup.send(
            f"A team called **{changename}** already exists. Pick a different name.", ephemeral=True
        )
        return

    normalized_colour = None
    if changecolour:
        normalized_colour = normalize_hex_colour(changecolour)
        if normalized_colour is None:
            await interaction.followup.send(
                "That's not a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
            )
            return

    if changeicon and not is_valid_standard_emoji(changeicon):
        await interaction.followup.send(
            "That's not a standard Discord emoji. Please use a single regular emoji "
            "(custom server emojis can't be used in channel names or role icons).",
            ephemeral=True,
        )
        return

    info = db["teams"][team_key]
    role = interaction.guild.get_role(info["role_id"])
    channel = interaction.guild.get_channel(info["channel_id"])

    new_name = changename if changename else team_key
    new_emoji = changeicon if changeicon else info["emoji"]

    role_edit_kwargs = {}
    if changename:
        role_edit_kwargs["name"] = f"{new_name} Team"
    if changecolour:
        role_edit_kwargs["colour"] = discord.Colour.from_str(normalized_colour)
    if changeicon:
        role_edit_kwargs["display_icon"] = new_emoji

    icon_warning = None
    if role and role_edit_kwargs:
        try:
            await role.edit(reason=f"Team settings changed by staff member {interaction.user}", **role_edit_kwargs)
        except discord.HTTPException:
            if "display_icon" in role_edit_kwargs:
                # Role icons require a certain server boost level; retry without it
                role_edit_kwargs.pop("display_icon")
                icon_warning = "couldn't set the role icon (requires a certain server boost level)"
                if role_edit_kwargs:
                    try:
                        await role.edit(
                            reason=f"Team settings changed by staff member {interaction.user}",
                            **role_edit_kwargs,
                        )
                    except discord.HTTPException:
                        await interaction.followup.send(
                            "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                        )
                        return
            else:
                await interaction.followup.send(
                    "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                )
                return

    if channel and (changename or changeicon):
        try:
            await channel.edit(
                name=f"{new_emoji}┃{new_name}-Team",
                reason=f"Team settings changed by staff member {interaction.user}",
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Updated the role, but couldn't rename the channel — Discord rejected the new "
                "name (check length/characters). Team may now be inconsistently named.",
                ephemeral=True,
            )
            return

    if changename and new_name.lower() != team_key.lower():
        db["teams"][new_name] = info
        del db["teams"][team_key]
        team_key = new_name
    if changeicon:
        db["teams"][team_key]["emoji"] = new_emoji

    save_db(db)
    await backup_db_to_log_channel()

    changes = []
    if changename:
        changes.append(f"name → **{new_name}**")
    if changecolour:
        changes.append(f"colour → `{normalized_colour}`")
    if changeicon:
        changes.append(f"icon → {new_emoji}")

    message = f"✅ Updated **{team_key}**: " + ", ".join(changes)
    if icon_warning:
        message += f"\n⚠️ Everything else applied, but {icon_warning}."
    await interaction.followup.send(message, ephemeral=True)


staffchangesetting.autocomplete("team")(team_name_autocomplete)


@bot.tree.command(
    name="cleanuporphanteams",
    description="(Staff) Delete channels/roles in the team category that have no matching database entry",
)
async def cleanuporphanteams(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    category = interaction.guild.get_channel(TEAM_CATEGORY_ID)
    if category is None or not isinstance(category, discord.CategoryChannel):
        await interaction.followup.send("Couldn't find the team category.", ephemeral=True)
        return

    db = load_db()
    known_channel_ids = {info["channel_id"] for info in db["teams"].values()}

    orphans = []  # list of (channel, role_or_None)
    for channel in category.channels:
        if channel.id in known_channel_ids:
            continue
        linked_role = None
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Role) and target.id != interaction.guild.default_role.id:
                allow, _deny = overwrite.pair()
                if allow.view_channel:
                    linked_role = target
                    break
        orphans.append((channel, linked_role))

    if not orphans:
        await interaction.followup.send(
            "No orphaned team channels found — everything in the category matches the database.",
            ephemeral=True,
        )
        return

    preview_limit = 20
    lines = []
    for channel, role in orphans[:preview_limit]:
        role_part = f" + role **{role.name}**" if role else " (no linked role found)"
        lines.append(f"• {channel.mention}{role_part}")
    if len(orphans) > preview_limit:
        lines.append(f"…and {len(orphans) - preview_limit} more")

    view = ConfirmCleanupView(interaction.user.id, orphans)
    await interaction.followup.send(
        f"Found **{len(orphans)}** channel(s) in the team category with no matching database "
        f"entry:\n" + "\n".join(lines) + "\n\nDelete them (and their linked roles)? This can't be undone.",
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="premiumteamsettings",
    description="(Premium) Apply gradient role colours or a custom role icon to your team",
)
@app_commands.describe(
    colour1="Primary role colour",
    colour1hex="Custom primary hex colour, e.g. #5865F2 — overrides colour1 if both are given",
    colour2="Secondary role colour — combined with colour1 this creates a gradient",
    colour2hex="Custom secondary hex colour, e.g. #5865F2 — overrides colour2 if both are given",
    roleicon="Image to use as the team role's icon",
)
@app_commands.choices(colour1=PREMIUM_COLOUR_CHOICES, colour2=PREMIUM_COLOUR_CHOICES)
async def premiumteamsettings(
    interaction: discord.Interaction,
    colour1: app_commands.Choice[str] = None,
    colour1hex: str = None,
    colour2: app_commands.Choice[str] = None,
    colour2hex: str = None,
    roleicon: discord.Attachment = None,
):
    await interaction.response.defer(ephemeral=True)

    if not has_premium_access(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to use this command.", ephemeral=True)
        return

    if not any([colour1, colour1hex, colour2, colour2hex, roleicon]):
        await interaction.followup.send(
            "You didn't specify anything to change. Provide a colour (dropdown or hex) and/or "
            "`roleicon`.",
            ephemeral=True,
        )
        return

    if roleicon is not None and not (roleicon.content_type or "").startswith("image/"):
        await interaction.followup.send("`roleicon` needs to be an image file.", ephemeral=True)
        return

    resolved_colour1 = None
    colour1_label = None
    if colour1hex:
        resolved_colour1 = normalize_hex_colour(colour1hex)
        if resolved_colour1 is None:
            await interaction.followup.send(
                "`colour1hex` isn't a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
            )
            return
        colour1_label = resolved_colour1
    elif colour1:
        resolved_colour1 = colour1.value
        colour1_label = colour1.name

    resolved_colour2 = None
    colour2_label = None
    if colour2hex:
        resolved_colour2 = normalize_hex_colour(colour2hex)
        if resolved_colour2 is None:
            await interaction.followup.send(
                "`colour2hex` isn't a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
            )
            return
        colour2_label = resolved_colour2
    elif colour2:
        resolved_colour2 = colour2.value
        colour2_label = colour2.name

    info = db["teams"][team_key]
    role = interaction.guild.get_role(info["role_id"])
    channel = interaction.guild.get_channel(info["channel_id"])
    if role is None:
        await interaction.followup.send("That team's role no longer exists.", ephemeral=True)
        return

    role_edit_kwargs = {}
    if resolved_colour1:
        role_edit_kwargs["colour"] = discord.Colour.from_str(resolved_colour1)
    if resolved_colour2:
        role_edit_kwargs["secondary_colour"] = discord.Colour.from_str(resolved_colour2)

    icon_warning = None
    if roleicon is not None:
        temp_emoji = None
        try:
            image_bytes = await roleicon.read()
            safe_name = re.sub(r"[^A-Za-z0-9_]", "", team_key)[:20] or "team"
            temp_emoji = await interaction.guild.create_custom_emoji(
                name=f"tmp_{safe_name}"[:32],
                image=image_bytes,
                reason="Temporary emoji used to process a premium role icon",
            )
            processed_bytes = await temp_emoji.read()
            role_edit_kwargs["display_icon"] = processed_bytes
        except discord.HTTPException:
            icon_warning = "couldn't process the role icon image"
        finally:
            if temp_emoji is not None:
                try:
                    await temp_emoji.delete(reason="Cleanup after setting premium role icon")
                except discord.HTTPException:
                    pass

    gradient_warning = None
    if role_edit_kwargs:
        try:
            await role.edit(reason=f"Premium settings changed by {interaction.user}", **role_edit_kwargs)
        except discord.HTTPException:
            # Gradients and role icons need a certain server boost level; fall back to just the
            # primary colour rather than losing the whole update.
            fallback_kwargs = {}
            if "colour" in role_edit_kwargs:
                fallback_kwargs["colour"] = role_edit_kwargs["colour"]
            if fallback_kwargs:
                try:
                    await role.edit(
                        reason=f"Premium settings changed by {interaction.user}", **fallback_kwargs
                    )
                    gradient_warning = (
                        "some of those changes need a higher server boost level and weren't applied"
                    )
                except discord.HTTPException:
                    await interaction.followup.send(
                        "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                    )
                    return
            else:
                await interaction.followup.send(
                    "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                )
                return

    first_activation = not info.get("premium", False)
    if first_activation:
        info["premium"] = True
        if channel:
            try:
                await channel.send(
                    "<:Camera:1528219214345666621> **Premium Activated!** "
                    "<:CompanyCoins:1528218837030535394>"
                )
            except discord.HTTPException:
                pass

    premium_marker_role = interaction.guild.get_role(PREMIUM_ROLE_ID)
    if premium_marker_role is not None:
        try:
            await role.edit(
                position=premium_marker_role.position + 1,
                reason="Keep premium team role above the premium marker role",
            )
        except discord.HTTPException:
            pass

    save_db(db)
    await backup_db_to_log_channel()

    changes = []
    if colour1_label:
        changes.append(f"colour1 → {colour1_label}")
    if colour2_label:
        changes.append(f"colour2 → {colour2_label}")
    if roleicon is not None and "display_icon" in role_edit_kwargs:
        changes.append("icon updated")

    message = (
        f"✨ Updated **{team_key}**'s premium styling: " + ", ".join(changes)
        if changes
        else f"✨ Premium settings applied for **{team_key}**."
    )
    if icon_warning:
        message += f"\n⚠️ {icon_warning.capitalize()}."
    if gradient_warning:
        message += f"\n⚠️ {gradient_warning.capitalize()}."
    await interaction.followup.send(message, ephemeral=True)


@bot.tree.command(
    name="changeteamsettings",
    description="Change your team's name, colour, or icon, or delete it (leader only)",
)
@app_commands.describe(
    delete="Delete your team — removes the role, channel, and database entry (can't be undone)",
    changename="New team name",
    changecolour="New hex colour for the team's role, e.g. #5865F2",
    changeicon="New single standard emoji for the team (no custom server emojis)",
)
async def changeteamsettings(
    interaction: discord.Interaction,
    delete: bool = False,
    changename: str = None,
    changecolour: str = None,
    changeicon: str = None,
):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    team_key = find_team_by_leader(db["teams"], interaction.user.id)
    if not team_key:
        await interaction.followup.send("You must be a team leader to use this command.", ephemeral=True)
        return

    if delete:
        view = ConfirmDeleteTeamView(interaction.user.id, team_key, interaction.guild)
        await interaction.followup.send(
            f"Are you sure you want to delete **{team_key}**? This will remove the team's role, "
            f"channel, and database entry, and can't be undone.",
            view=view,
            ephemeral=True,
        )
        return

    if not any([changename, changecolour, changeicon]):
        await interaction.followup.send(
            "You didn't specify anything to change. Provide `changename`, `changecolour`, "
            "`changeicon`, or set `delete:` to True.",
            ephemeral=True,
        )
        return

    if changename and changename.lower() != team_key.lower() and find_team_key_ci(db["teams"], changename):
        await interaction.followup.send(
            f"A team called **{changename}** already exists. Pick a different name.", ephemeral=True
        )
        return

    normalized_colour = None
    if changecolour:
        normalized_colour = normalize_hex_colour(changecolour)
        if normalized_colour is None:
            await interaction.followup.send(
                "That's not a valid hex colour. Use a format like `#5865F2`.", ephemeral=True
            )
            return

    if changeicon and not is_valid_standard_emoji(changeicon):
        await interaction.followup.send(
            "That's not a standard Discord emoji. Please use a single regular emoji "
            "(custom server emojis can't be used in channel names or role icons).",
            ephemeral=True,
        )
        return

    info = db["teams"][team_key]
    role = interaction.guild.get_role(info["role_id"])
    channel = interaction.guild.get_channel(info["channel_id"])

    new_name = changename if changename else team_key
    new_emoji = changeicon if changeicon else info["emoji"]

    role_edit_kwargs = {}
    if changename:
        role_edit_kwargs["name"] = f"{new_name} Team"
    if changecolour:
        role_edit_kwargs["colour"] = discord.Colour.from_str(normalized_colour)
    if changeicon:
        role_edit_kwargs["display_icon"] = new_emoji

    icon_warning = None
    if role and role_edit_kwargs:
        try:
            await role.edit(reason=f"Team settings changed by {interaction.user}", **role_edit_kwargs)
        except discord.HTTPException:
            if "display_icon" in role_edit_kwargs:
                # Role icons require a certain server boost level; retry without it
                role_edit_kwargs.pop("display_icon")
                icon_warning = "couldn't set the role icon (requires a certain server boost level)"
                if role_edit_kwargs:
                    try:
                        await role.edit(
                            reason=f"Team settings changed by {interaction.user}",
                            **role_edit_kwargs,
                        )
                    except discord.HTTPException:
                        await interaction.followup.send(
                            "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                        )
                        return
            else:
                await interaction.followup.send(
                    "Couldn't apply those changes — Discord rejected the request.", ephemeral=True
                )
                return

    if channel and (changename or changeicon):
        try:
            await channel.edit(
                name=f"{new_emoji}┃{new_name}-Team",
                reason=f"Team settings changed by {interaction.user}",
            )
        except discord.HTTPException:
            await interaction.followup.send(
                "Updated the role, but couldn't rename the channel — Discord rejected the new "
                "name (check length/characters). Team may now be inconsistently named.",
                ephemeral=True,
            )
            return

    if changename and new_name.lower() != team_key.lower():
        db["teams"][new_name] = info
        del db["teams"][team_key]
        team_key = new_name
    if changeicon:
        db["teams"][team_key]["emoji"] = new_emoji

    save_db(db)
    await backup_db_to_log_channel()

    changes = []
    if changename:
        changes.append(f"name → **{new_name}**")
    if changecolour:
        changes.append(f"colour → `{normalized_colour}`")
    if changeicon:
        changes.append(f"icon → {new_emoji}")

    message = f"✅ Updated **{team_key}**: " + ", ".join(changes)
    if icon_warning:
        message += f"\n⚠️ Everything else applied, but {icon_warning}."
    await interaction.followup.send(message, ephemeral=True)


# ---------- Giveaway slash command ----------
@bot.tree.command(name="startgiveaway", description="(Staff) Start a giveaway in this channel")
@app_commands.describe(
    winners="How many winners will be picked",
    prize="What's being given away",
    ends="How long the giveaway runs, e.g. 10m, 2h, 1d, 1d12h",
    hosted="Who's hosting the giveaway (defaults to you)",
)
async def startgiveaway(
    interaction: discord.Interaction,
    winners: app_commands.Range[int, 1, 50],
    prize: str,
    ends: str,
    hosted: discord.Member = None,
):
    await interaction.response.defer(ephemeral=True)

    if not has_staff_role(interaction.user):
        await interaction.followup.send("You don't have permission to use this command.", ephemeral=True)
        return

    duration = parse_duration(ends)
    if duration is None:
        await interaction.followup.send(
            "Couldn't parse `ends` — use something like `10m`, `2h`, `1d`, or `1d12h`.", ephemeral=True
        )
        return

    host = hosted or interaction.user
    end_dt = discord.utils.utcnow() + duration
    end_ts = int(end_dt.timestamp())

    embed = build_giveaway_embed(
        prize=prize, winners_count=winners, host_id=host.id, end_ts=end_ts, entries_count=0,
    )
    view = GiveawayJoinView()

    if os.path.exists(SUPPORT_BANNER_PATH):
        file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)
        sent = await interaction.channel.send(embed=embed, view=view, file=file)
    else:
        embed.set_image(url=None)
        sent = await interaction.channel.send(embed=embed, view=view)

    db = load_db()
    db.setdefault("giveaways", {})
    db["giveaways"][str(sent.id)] = {
        "guild_id": interaction.guild.id,
        "channel_id": sent.channel.id,
        "prize": prize,
        "winners": winners,
        "host_id": host.id,
        "end_ts": end_ts,
        "entries": [],
        "ended": False,
    }
    save_db(db)
    await backup_db_to_log_channel()

    await interaction.followup.send(f"{GIVEAWAY_JOIN_EMOJI} Giveaway started in {sent.channel.mention}!", ephemeral=True)


# ---------- /test command (admin-only — posts the update embed in the update channel) ----------
@bot.tree.command(name="testupdate", description="(Admin) Post a test update embed in the update channel")
@app_commands.default_permissions(administrator=True)
async def testupdate(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "Only admins can use this command.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    # Hit the live API so the embed shows real, current data
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                OCULUS_VERSIONS_URL, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                if response.status != 200:
                    await interaction.followup.send(
                        f"API request failed (HTTP {response.status}).", ephemeral=True
                    )
                    return
                payload = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError) as e:
        await interaction.followup.send(
            f"API request failed: {e}", ephemeral=True
        )
        return

    latest_entry = _extract_latest_oculus_version_entry(payload)
    if latest_entry is None:
        await interaction.followup.send(
            "No version entries found in the API response.", ephemeral=True
        )
        return

    display_version = _extract_oculus_display_version(latest_entry)
    release_ts = _extract_release_timestamp(latest_entry)

    state = load_oculus_version_state()
    previous_display = state.get("previous_display_version") or "N/A"

    embed = _build_update_embed(
        display_version=display_version,
        previous_version=previous_display,
        release_ts=release_ts,
    )

    file = None
    if os.path.exists(SUPPORT_BANNER_PATH):
        file = discord.File(SUPPORT_BANNER_PATH, filename=SUPPORT_BANNER_FILENAME)

    channel = bot.get_channel(OCULUS_UPDATE_CHANNEL_ID) or await bot.fetch_channel(OCULUS_UPDATE_CHANNEL_ID)

    if file:
        await channel.send(embed=embed, file=file)
    else:
        await channel.send(embed=embed)

    await interaction.followup.send(
        f"Test update posted in {channel.mention}.", ephemeral=True
    )


# ---------- Bracket slash command ----------
@bot.tree.command(name="bracket", description="Create a new tournament bracket (8 teams, single elimination)")
async def bracket_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    db = load_db()
    existing = load_bracket(db)
    if existing:
        await interaction.followup.send(
            "A bracket already exists. Use the panel buttons to manage it, or ask staff to clear it first.",
            ephemeral=True,
        )
        return

    new_bracket = BracketState()
    save_bracket(db, new_bracket)
    save_db(db)

    await _refresh_bracket_panel(interaction.guild, new_bracket)

    generate_bracket_image(new_bracket, BRACKET_IMAGE_PATH)
    await _refresh_bracket_public(interaction.guild, new_bracket)

    await interaction.followup.send(
        "✅ Bracket created! Use the panel in the bracket channel to add participants.",
        ephemeral=True,
    )


@bot.event
async def on_ready():
    await restore_db_from_log_channel()
    bot.add_view(SupportPanelView())
    bot.add_view(TournamentTeamSelectView(keep_nav_buttons=True))
    bot.add_view(TournamentSubmissionView())
    bot.add_view(CodeRecipientSelectView([], keep_nav_buttons=True))
    bot.add_view(BracketPanelView())
    bot.add_view(GiveawayJoinView())
    await bot.tree.sync()
    try:
        await sync_existing_teams()
    except discord.HTTPException as e:
        print(f"Failed to sync existing teams (leader role/permissions): {e}")
    try:
        await refresh_support_ticket_panel()
    except discord.HTTPException as e:
        print(f"Failed to refresh support ticket panel: {e}")
    try:
        await refresh_tournament_panel()
    except discord.HTTPException as e:
        print(f"Failed to refresh tournament panel: {e}")
    try:
        # Sync bracket channels — fetches the guild from any available channel
        _guild = None
        try:
            ch = bot.get_channel(BRACKET_PANEL_CHANNEL_ID) or await bot.fetch_channel(BRACKET_PANEL_CHANNEL_ID)
            _guild = ch.guild
        except discord.HTTPException:
            pass
        if _guild:
            await sync_bracket(_guild)
    except discord.HTTPException as e:
        print(f"Failed to sync bracket: {e}")
    if not check_oculus_updates.is_running():
        check_oculus_updates.start()
    if not check_giveaways.is_running():
        check_giveaways.start()
    print(f"Logged in as {bot.user} (id: {bot.user.id})")
    print("Slash commands synced.")


if __name__ == "__main__":
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN environment variable is not set")
    bot.run(token)
