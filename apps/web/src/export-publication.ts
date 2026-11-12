type ExportState = {state: string};

// The durable publication checkpoint can briefly be visible before ready.
// Read it again, without requesting another render or resolving a real unknown.
export async function readPublishedExports<T extends ExportState>(
  read: () => Promise<T[]>, signal: AbortSignal,
  pause = () => new Promise<void>(resolve => setTimeout(resolve, 1200)),
): Promise<T[]> {
  for (let attempt = 0; ; attempt++) {
    signal.throwIfAborted();
    const rows = await read();
    signal.throwIfAborted();
    if (!rows.some(row => row.state === 'recovery_required') || attempt === 5) return rows;
    await pause();
  }
}
