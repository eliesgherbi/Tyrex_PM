import { Component, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";

class Boundary extends Component<{ children: ReactNode }, { message: string }> {
  state = { message: "" };
  static getDerivedStateFromError(error: Error) {
    return { message: error.message };
  }
  render() {
    if (this.state.message) {
      return <p className="inline-warning">The workspace view stopped: {this.state.message}</p>;
    }
    return this.props.children;
  }
}

createRoot(document.getElementById("root")!).render(<Boundary><App /></Boundary>);
