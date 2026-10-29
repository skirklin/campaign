// Dice: parsing and rolling expressions like "2d6+3", "8d6 fire", "2d20kh1+5"
// (advantage: keep the highest one) and "4d6kh3" (rolling a stat).

export function d(sides) {
  const buf = new Uint32Array(1);
  crypto.getRandomValues(buf);
  return 1 + (buf[0] % sides);
}

export const fmtMod = (m) => (m ? `${m > 0 ? "+" : "−"} ${Math.abs(m)}` : "");

const TERM = /([+-]?)(?:(\d*)d(\d+)(?:k([hl])(\d+))?|(\d+))/gi;

// "2d6+1d4+3 fire" -> {dice: [{n: 2, sides: 6}, {n: 1, sides: 4}], mod: 3, rest: "fire"}.
// Each die term may keep only its highest or lowest: {n: 2, sides: 20, keep: ["h", 1]}.
// Null if there's nothing to roll.
export function parseExpr(text) {
  const m = String(text || "").match(/^\s*((?:[+-]?\s*(?:\d*d\d+(?:k[hl]\d+)?|\d+)\s*)+)(.*)$/i);
  if (!m) return null;
  const dice = [];
  let mod = 0;
  for (const t of m[1].replace(/\s+/g, "").matchAll(TERM)) {
    const [, sgn, n, sides, hl, k, num] = t;
    const s = sgn === "-" ? -1 : 1;
    if (num !== undefined) {
      mod += s * Number(num);
      continue;
    }
    const die = { n: s * (Number(n) || 1), sides: Number(sides) };
    if (!die.sides || Math.abs(die.n) > 100 || die.sides > 1000) return null;
    if (hl) die.keep = [hl.toLowerCase(), Math.min(Number(k), Math.abs(die.n))];
    dice.push(die);
  }
  return { dice, mod, rest: m[2].trim() };
}

export function formatExpr({ dice, mod }) {
  const terms = dice.map((t, i) => {
    const body = `${Math.abs(t.n)}d${t.sides}${t.keep ? `k${t.keep[0]}${t.keep[1]}` : ""}`;
    return (t.n < 0 ? "-" : i ? "+" : "") + body;
  });
  if (mod) terms.push(`${mod < 0 ? "-" : terms.length ? "+" : ""}${Math.abs(mod)}`);
  return terms.join("") || "0";
}

// Roll a parsed expression; `crit` doubles the dice. -> {total, detail, natural}
// `natural` is the kept d20 when the expression is a single d20 roll (for crits and fumbles).
export function rollExpr({ dice, mod }, { crit = false } = {}) {
  let total = mod;
  const shown = [];
  let natural = null;
  for (const t of dice) {
    const count = Math.abs(t.n) * (crit ? 2 : 1);
    const rolls = Array.from({ length: count }, () => d(t.sides));
    let kept = rolls.map((_, i) => i);
    if (t.keep) {
      const order = kept.slice().sort((a, b) => (t.keep[0] === "h" ? rolls[b] - rolls[a] : rolls[a] - rolls[b]));
      kept = order.slice(0, t.keep[1]);
    }
    const sum = kept.reduce((a, i) => a + rolls[i], 0);
    total += Math.sign(t.n) * sum;
    if (t.sides === 20 && dice.length === 1 && kept.length === 1) natural = rolls[kept[0]];
    const faces = rolls.map((r, i) => (kept.includes(i) ? r : `(${r})`)).join(", ");
    const body = `${count}d${t.sides}${t.keep ? `k${t.keep[0]}${t.keep[1]}` : ""}`;
    shown.push(`${t.n < 0 ? "− " : shown.length ? "+ " : ""}${body} [${faces}]`);
  }
  return { total, detail: (shown.join(" ") + (mod ? ` ${fmtMod(mod)}` : "")).trim(), natural };
}
