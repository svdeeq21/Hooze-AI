import { Component, type ReactNode } from "react";

/** Catches render-time crashes so one bad tab shows a message instead of "Something went wrong!" for the whole app.
 *  `resetKey` changes (e.g. switching client/tab) -> the boundary clears itself. */
export class ErrorBoundary extends Component<{ children: ReactNode; resetKey?: string; label?: string }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  componentDidUpdate(prev: { resetKey?: string }) { if (prev.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null }); }
  componentDidCatch(error: Error) { console.error(`[${this.props.label ?? "ui"}]`, error); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="rounded-xl border border-bad/30 bg-bad/10 p-4 text-sm">
        <div className="font-medium text-bad">{this.props.label ?? "This section"} hit a problem and was stopped.</div>
        <p className="mt-1 text-muted">Nothing was saved or changed. The rest of the dashboard still works. If you just pasted a configuration, it probably has a field in the wrong shape. Use “Copy schema prompt” in Advanced: JSON to regenerate it.</p>
        <pre className="mt-2 overflow-x-auto rounded-lg bg-ink/40 p-2 text-xs text-bad">{this.state.error.message}</pre>
        <button onClick={() => this.setState({ error: null })} className="mt-3 rounded-lg border border-line bg-raised px-3 py-1.5 text-xs cursor-pointer hover:bg-line">Try again</button>
      </div>
    );
  }
}
