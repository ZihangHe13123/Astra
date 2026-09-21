type Schedule = (callback: () => void, delay: number) => () => void;

/** First IPC response has a deadline; model loading is a separate phase. */
export function createBackendHandshake(
  warn: () => void,
  expire: () => void,
  schedule: Schedule = (callback, delay) => {
    const timer = setTimeout(callback, delay);
    return () => clearTimeout(timer);
  },
) {
  let complete = false;
  const close = () => {
    complete = true;
    cancelWarning();
    cancelDeadline();
  };
  const cancelWarning = schedule(() => {
    if (!complete) warn();
  }, 3000);
  const cancelDeadline = schedule(() => {
    if (complete) return;
    close();
    expire();
  }, 15000);
  return close;
}
