import React, { useState, useEffect, useRef, useCallback } from "react";
import { DevLogConsole } from "../../components/DevLogConsole";
import { openDevLogsWindow, type LogLevel } from "../../api";

interface DevLogDrawerProps {
  isOpen: boolean;
  onClose: () => void;
  initialLevel?: LogLevel | "ALL";
}

const DEFAULT_HEIGHT = 280;
const MIN_HEIGHT = 160;

export function DevLogDrawer({
  isOpen,
  onClose,
  initialLevel = "ALL",
}: DevLogDrawerProps) {
  const [height, setHeight] = useState<number>(() => {
    try {
      const saved = localStorage.getItem("dev-logs-drawer-height");
      if (saved) {
        const parsed = parseInt(saved, 10);
        if (!isNaN(parsed) && parsed >= MIN_HEIGHT) return parsed;
      }
    } catch {
      // Ignore
    }
    return DEFAULT_HEIGHT;
  });

  const isDraggingRef = useRef<boolean>(false);
  const startYRef = useRef<number>(0);
  const startHeightRef = useRef<number>(DEFAULT_HEIGHT);

  const handleMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    isDraggingRef.current = true;
    startYRef.current = e.clientY;
    startHeightRef.current = height;
    document.body.style.cursor = "row-resize";
    document.body.style.userSelect = "none";
  };

  const handleMouseMove = useCallback((e: MouseEvent) => {
    if (!isDraggingRef.current) return;
    const deltaY = startYRef.current - e.clientY;
    const maxHeight = window.innerHeight * 0.75;
    const newHeight = Math.min(Math.max(startHeightRef.current + deltaY, MIN_HEIGHT), maxHeight);
    setHeight(newHeight);
  }, []);

  const handleMouseUp = useCallback(() => {
    if (isDraggingRef.current) {
      isDraggingRef.current = false;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      try {
        localStorage.setItem("dev-logs-drawer-height", String(height));
      } catch {
        // Ignore
      }
    }
  }, [height]);

  useEffect(() => {
    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);
    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [handleMouseMove, handleMouseUp]);

  const handlePopOut = async () => {
    onClose();
    try {
      await openDevLogsWindow();
    } catch (err) {
      console.error("Could not open secondary dev-logs window:", err);
    }
  };

  if (!isOpen) return null;

  return (
    <div
      style={{
        position: "relative",
        height: `${height}px`,
        width: "100%",
        display: "flex",
        flexDirection: "column",
        zIndex: 40,
        boxShadow: "0 -4px 20px rgba(0,0,0,0.5)",
      }}
    >
      {/* Drag handle */}
      <div
        onMouseDown={handleMouseDown}
        style={{
          height: "6px",
          width: "100%",
          backgroundColor: "#1e293b",
          cursor: "row-resize",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          borderTop: "1px solid var(--line-12)",
          borderBottom: "1px solid rgba(0,0,0,0.3)",
          transition: "background-color 0.15s",
        }}
        onMouseEnter={(e) => {
          e.currentTarget.style.backgroundColor = "#3b82f6";
        }}
        onMouseLeave={(e) => {
          e.currentTarget.style.backgroundColor = "#1e293b";
        }}
      >
        <div
          style={{
            width: "36px",
            height: "2px",
            backgroundColor: "var(--surface-20)",
            borderRadius: "1px",
          }}
        />
      </div>

      {/* Main Console inside drawer */}
      <div style={{ flex: 1, minHeight: 0 }}>
        <DevLogConsole
          standalone={false}
          onPopOut={handlePopOut}
          onClose={onClose}
          initialLevel={initialLevel}
        />
      </div>
    </div>
  );
}
