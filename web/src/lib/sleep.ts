export function sleep(ms: number, reduced: boolean): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, reduced ? 0 : ms));
}
