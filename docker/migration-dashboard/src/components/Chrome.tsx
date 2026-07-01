import type { DisplayStatus } from "../hooks/useLiveRun";
import logomark from "../assets/logomark.svg";

interface Props {
  status: DisplayStatus;
}

const STATUS_TONES: Record<DisplayStatus, { label: string; tone: string }> = {
  running: { label: "Migrating · step 2", tone: "running" },
  paused: { label: "Paused", tone: "paused" },
  done: { label: "Done · step 3 ready", tone: "done" },
  failed: { label: "Failed", tone: "failed" },
  idle: { label: "Idle", tone: "idle" },
};

export function Chrome({ status }: Props) {
  const s = STATUS_TONES[status] ?? STATUS_TONES.idle;
  return (
    <div className="chrome">
      <div className="chrome-left">
        <div className="brand">
          <img
            src={logomark}
            alt="MigrationRoom"
            className="brand-logomark"
            width={28}
            height={28}
          />
          <span className="brand-name">
            <b>Migration</b>House
          </span>
        </div>
        <span className="brand-tagline">
          AI-assisted migration to ClickHouse Cloud
        </span>
      </div>
      <div className="chrome-right">
        <span className="chrome-status-pill" data-tone={s.tone}>
          <span className="dot" />
          {s.label}
        </span>
        <span style={{ color: "#3D3D3D" }}>·</span>
        <span style={{ fontFamily: "var(--font-mono)" }}>
          admin@playground.local
        </span>
      </div>
    </div>
  );
}
