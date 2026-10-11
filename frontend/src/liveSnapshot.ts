/** Cancel results belonging to a previous connection or selected resource. */
export function loadSnapshot<T>(fetch: () => Promise<T>, apply: (snapshot: T) => void, fail: (error: Error) => void) {
  let cancelled = false;
  fetch().then(value => { if (!cancelled) apply(value); }, error => { if (!cancelled) fail(error); });
  return () => { cancelled = true; };
}
