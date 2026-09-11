(function (root) {
  "use strict";

  function clone(value) { return JSON.parse(JSON.stringify(value)); }
  function serial(value) { return JSON.stringify(value); }

  /** Bounded, in-memory history for one unsaved BUILD draft.
   *
   * State contains workflow semantics and draft layout only. Runtime overlays,
   * run ids, journals and Git state are deliberately impossible to put here.
   */
  class DraftHistory {
    constructor(limit) {
      this.limit = Math.max(1, Number(limit) || 100);
      this.undoStack = [];
      this.redoStack = [];
      this.current = null;
    }

    reset(state) {
      this.current = clone(state);
      this.undoStack = [];
      this.redoStack = [];
      return clone(this.current);
    }

    record(state, label) {
      const next = clone(state);
      if (this.current !== null && serial(next) === serial(this.current)) return false;
      if (this.current !== null) {
        this.undoStack.push({ state: this.current, label: String(label || "edit") });
        if (this.undoStack.length > this.limit) this.undoStack.shift();
      }
      this.current = next;
      this.redoStack = [];
      return true;
    }

    undo() {
      if (!this.undoStack.length) return null;
      const previous = this.undoStack.pop();
      this.redoStack.push({ state: this.current, label: previous.label });
      this.current = clone(previous.state);
      return { state: clone(this.current), label: previous.label };
    }

    redo() {
      if (!this.redoStack.length) return null;
      const next = this.redoStack.pop();
      this.undoStack.push({ state: this.current, label: next.label });
      this.current = clone(next.state);
      return { state: clone(this.current), label: next.label };
    }

    canUndo() { return this.undoStack.length > 0; }
    canRedo() { return this.redoStack.length > 0; }
  }

  root.DraftHistory = DraftHistory;
  if (typeof module !== "undefined" && module.exports) module.exports = { DraftHistory };
})(typeof window !== "undefined" ? window : globalThis);
