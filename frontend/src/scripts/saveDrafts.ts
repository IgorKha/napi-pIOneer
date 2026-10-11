export interface Draft { text: string; saved: string; binary: boolean }

/** Drain edits made while a write or tree refresh is in flight before Run proceeds. */
export async function saveDrafts(
  snapshot: () => Record<string, Draft>,
  write: (path: string, text: string) => Promise<unknown>,
  acknowledge: (path: string, text: string) => void,
  refresh: () => Promise<void>,
): Promise<void> {
  do {
    for (const [path, draft] of Object.entries(snapshot())) {
      if (draft.binary || draft.text === draft.saved) continue;
      await write(path, draft.text);
      acknowledge(path, draft.text);
    }
    await refresh();
  } while (Object.values(snapshot()).some(d => !d.binary && d.text !== d.saved));
}
