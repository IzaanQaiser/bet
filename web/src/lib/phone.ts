// North American numbers only, matching every real number used across
// this project (+1). Display state is kept as raw typed digits (up to
// 10), formatted fresh on every render — the E.164 form the backend
// actually wants is only derived at submit time via toE164().

export function formatPhoneDisplay(digits: string): string {
  const d = digits.slice(0, 10);
  if (d.length === 0) return "";
  if (d.length < 4) return `(${d}`;
  if (d.length < 7) return `(${d.slice(0, 3)}) ${d.slice(3)}`;
  return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`;
}

export function digitsOnly(input: string): string {
  return input.replace(/\D/g, "").slice(0, 10);
}

export function toE164(digits: string): string {
  return digits.length === 10 ? `+1${digits}` : "";
}
