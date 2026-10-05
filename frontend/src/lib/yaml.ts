// A small YAML writer for the "this is what will be saved" preview. The server writes the real file.

const PLAIN = /^[A-Za-z0-9_./~@${}:+-][A-Za-z0-9_ ./~@${}:+-]*$/;
const RESERVED = /^(true|false|yes|no|on|off|null|~|-?\d+(\.\d+)?)$/i;

function scalar(v: unknown): string {
  if (v === null || v === undefined) return "null";
  if (typeof v === "boolean" || typeof v === "number") return String(v);
  const s = String(v);
  if (s === "" || RESERVED.test(s) || !PLAIN.test(s) || s.includes(": ") || s.endsWith(":") || s.startsWith("- ")) return JSON.stringify(s);
  return s;
}

export function toYaml(value: unknown, indent = 0): string {
  const pad = "  ".repeat(indent);
  if (Array.isArray(value)) {
    if (!value.length) return "[]";
    if (value.every((x) => x === null || typeof x !== "object")) return `[${value.map(scalar).join(", ")}]`;
    return value
      .map((x) => {
        const inner = toYaml(x, indent + 1).trimStart();
        return `\n${pad}- ${inner}`;
      })
      .join("");
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== undefined);
    if (!entries.length) return "{}";
    return entries
      .map(([k, v]) => {
        const key = scalar(k);
        if (v && typeof v === "object" && !(Array.isArray(v) && (!v.length || v.every((x) => x === null || typeof x !== "object"))) && Object.keys(v).length) {
          return `\n${pad}${key}:${toYaml(v, indent + 1)}`;
        }
        return `\n${pad}${key}: ${toYaml(v, indent + 1)}`;
      })
      .join("");
  }
  return scalar(value);
}

export function dumpYaml(value: unknown): string {
  return toYaml(value).replace(/^\n/, "") + "\n";
}
