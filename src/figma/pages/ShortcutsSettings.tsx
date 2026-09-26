// Settings → Shortcuts: see every keyboard shortcut, change any of them, and
// find out immediately when two commands want the same keys.
import React, { useMemo, useState } from "react";
import {
  HOTKEY_COMMANDS,
  HOTKEY_GROUPS,
  assignBinding,
  bindingFromEvent,
  bindingsFor,
  clearBinding,
  findConflicts,
  formatBinding,
  resetBinding,
  type HotkeyCommand,
  type HotkeyMap,
} from "../../lib/hotkeys";
import { Icon, ic } from "../icons";

function BindingChip({ binding, conflicted }: { binding: string; conflicted: boolean }) {
  return (
    <kbd
      className="shortcut-chip"
      style={conflicted ? { borderColor: "rgba(251,191,36,0.55)", color: "var(--warning)" } : undefined}
    >
      {formatBinding(binding)}
    </kbd>
  );
}

function CommandRow({
  command,
  map,
  conflicts,
  recording,
  onRecord,
  onChange,
}: {
  command: HotkeyCommand;
  map: HotkeyMap;
  conflicts: Record<string, string[]>;
  recording: boolean;
  onRecord: (id: string | null) => void;
  onChange: (next: HotkeyMap) => void;
}) {
  const bindings = bindingsFor(command, map);
  const customised = Boolean(map[command.id]);
  const clash = bindings.find((binding) => conflicts[binding]);
  const clashingWith = clash
    ? conflicts[clash]
        .filter((id) => id !== command.id)
        .map((id) => HOTKEY_COMMANDS.find((item) => item.id === id)?.label ?? id)
    : [];

  return (
    <div className="shortcut-row">
      <div className="min-w-0">
        <p className="shortcut-label">{command.label}</p>
        {command.description && <p className="shortcut-sub">{command.description}</p>}
        {clashingWith.length > 0 && (
          <p className="shortcut-warning">
            <Icon d={ic.alertTriangle} size={10} />
            Also used by {clashingWith.join(", ")}
          </p>
        )}
      </div>

      <div className="flex items-center gap-1.5 shrink-0">
        {recording ? (
          <span
            className="shortcut-chip recording"
            tabIndex={0}
            ref={(node) => node?.focus()}
            onBlur={() => onRecord(null)}
            onKeyDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
              if (event.key === "Escape") {
                onRecord(null);
                return;
              }
              const binding = bindingFromEvent(event.nativeEvent);
              if (!binding) return;
              onChange(assignBinding(map, command.id, binding));
              onRecord(null);
            }}
          >
            Press keys…
          </span>
        ) : bindings.length ? (
          bindings.map((binding) => (
            <BindingChip key={binding} binding={binding} conflicted={Boolean(conflicts[binding])} />
          ))
        ) : (
          <span className="shortcut-chip empty">Not set</span>
        )}

        {command.fixed ? (
          <span className="shortcut-note">Fixed</span>
        ) : (
          <>
            <button
              type="button"
              className="shortcut-action"
              onClick={() => onRecord(command.id)}
              title={`Set a new shortcut for ${command.label}`}
            >
              Change
            </button>
            {bindings.length > 0 && (
              <button
                type="button"
                className="shortcut-action"
                onClick={() => onChange(clearBinding(map, command.id))}
                title="Remove this shortcut"
                aria-label={`Remove shortcut for ${command.label}`}
              >
                <Icon d={ic.close} size={10} />
              </button>
            )}
            {customised && (
              <button
                type="button"
                className="shortcut-action"
                onClick={() => onChange(resetBinding(map, command.id))}
                title="Back to the default shortcut"
                aria-label={`Reset shortcut for ${command.label}`}
              >
                <Icon d={ic.refreshCw} size={10} />
              </button>
            )}
          </>
        )}
      </div>
    </div>
  );
}

export function ShortcutsSettings({
  map,
  onChange,
}: {
  map: HotkeyMap;
  onChange: (next: HotkeyMap) => void;
}) {
  const [recording, setRecording] = useState<string | null>(null);
  const conflicts = useMemo(() => findConflicts(map), [map]);
  const customisedCount = Object.keys(map).length;

  return (
    <>
      <div className="shortcut-intro">
        <div className="min-w-0">
          <p style={{ fontSize: "11.5px", color: "var(--ink-70)", margin: 0 }}>
            Click <strong style={{ color: "var(--ink-90)" }}>Change</strong> and press the keys you want. Escape cancels.
          </p>
          <p style={{ fontSize: "10.5px", color: "var(--ink-40)", margin: "2px 0 0" }}>
            Shortcuts do nothing while you are typing in a box, so they never eat your text.
          </p>
        </div>
        {customisedCount > 0 && (
          <button type="button" className="shortcut-action" onClick={() => onChange({})}>
            Reset all
          </button>
        )}
      </div>

      {HOTKEY_GROUPS.map((group) => {
        const commands = HOTKEY_COMMANDS.filter((command) => command.group === group);
        if (!commands.length) return null;
        return (
          <section key={group} className="shortcut-group">
            <p className="shortcut-group-title">{group}</p>
            <div className="shortcut-card">
              {commands.map((command) => (
                <CommandRow
                  key={command.id}
                  command={command}
                  map={map}
                  conflicts={conflicts}
                  recording={recording === command.id}
                  onRecord={setRecording}
                  onChange={onChange}
                />
              ))}
            </div>
          </section>
        );
      })}
    </>
  );
}
