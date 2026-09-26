import { Component, ErrorInfo, ReactNode } from "react";
import { emitLog } from "../lib/telemetry";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export class TelemetryErrorBoundary extends Component<Props, State> {
  public state: State = {
    hasError: false,
    error: null,
  };

  public static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  public componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error("TelemetryErrorBoundary caught error:", error, errorInfo);
    emitLog(
      "ERROR",
      "ui:render_crash",
      error.message || "React Component Render Crash",
      {
        componentStack: errorInfo.componentStack,
      },
      error
    );
  }

  private handleReset = () => {
    this.setState({ hasError: false, error: null });
    window.location.reload();
  };

  public render(): ReactNode {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }
      return (
        <div style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          height: "100vh",
          width: "100vw",
          backgroundColor: "#0f172a",
          color: "#f8fafc",
          fontFamily: "system-ui, -apple-system, sans-serif",
          padding: "24px",
          boxSizing: "border-box",
          textAlign: "center",
        }}>
          <div style={{
            fontSize: "48px",
            marginBottom: "16px",
          }}>⚠️</div>
          <h1 style={{
            fontSize: "20px",
            fontWeight: 600,
            marginBottom: "8px",
            color: "#ef4444",
          }}>
            Application Error Encountered
          </h1>
          <p style={{
            fontSize: "14px",
            color: "#94a3b8",
            maxWidth: "480px",
            marginBottom: "20px",
            lineHeight: 1.5,
          }}>
            An unexpected error interrupted the user interface. The error and diagnostic details have been automatically recorded to the diagnostic log.
          </p>
          {this.state.error && (
            <pre style={{
              backgroundColor: "#1e293b",
              color: "#fca5a5",
              padding: "12px 16px",
              borderRadius: "6px",
              fontSize: "12px",
              maxWidth: "600px",
              maxHeight: "180px",
              overflowX: "auto",
              marginBottom: "24px",
              textAlign: "left",
            }}>
              {this.state.error.message}
            </pre>
          )}
          <button
            onClick={this.handleReset}
            style={{
              backgroundColor: "#3b82f6",
              color: "#ffffff",
              border: "none",
              borderRadius: "6px",
              padding: "10px 20px",
              fontSize: "14px",
              fontWeight: 500,
              cursor: "pointer",
              boxShadow: "0 2px 4px rgba(0,0,0,0.2)",
            }}
          >
            Reload Application
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
