// Every icon in the design, as small React components.
// Stroke icons use currentColor, so they take the text color of whatever they sit in.

import type { ReactNode } from 'react';

type IconProps = { size?: number; stroke?: number; color?: string };

function Stroke({ size = 18, stroke = 1.75, color = 'currentColor', children }: IconProps & { children: ReactNode }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke={color}
      strokeWidth={stroke}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      style={{ flexShrink: 0 }}
    >
      {children}
    </svg>
  );
}

/** The Compass "Bearing" mark on its dark tile (header, 24px). */
export function MarkTile({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 128 128" aria-hidden="true" style={{ flexShrink: 0 }}>
      <rect width="128" height="128" rx="30" fill="#17160F" />
      <g transform="rotate(45 60 68)">
        <path d="M60 32 L74 68 L46 68 Z" fill="#FFFDF8" />
        <path d="M46 68 L74 68 L60 104 Z" fill="#FFFDF8" fillOpacity="0.42" />
      </g>
      <path d="M84 30 H98 V44" fill="none" stroke="#FFFDF8" strokeWidth="9" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** The mark without its tile — used as Compass's "voice" next to answers. */
export function Mark({ size = 18, color = '#17160F', needleOpacity = 0.35 }: { size?: number; color?: string; needleOpacity?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 128 128" aria-hidden="true" style={{ flexShrink: 0 }}>
      <g transform="rotate(45 60 68)">
        <path d="M60 22 L78 68 L42 68 Z" fill={color} />
        <path d="M42 68 L78 68 L60 114 Z" fill={color} fillOpacity={needleOpacity} />
      </g>
      <path d="M88 20 H108 V40" fill="none" stroke={color} strokeWidth="12" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

/** Greyed-out mark with a dotted bracket, for "can't read this page". */
export function MarkDisabled() {
  return (
    <svg width="64" height="64" viewBox="0 0 128 128" aria-hidden="true">
      <rect width="128" height="128" rx="30" fill="#EFEBE1" stroke="#D3CBB9" strokeWidth="2" />
      <g transform="rotate(45 60 68)">
        <path d="M60 32 L74 68 L46 68 Z" fill="#9A9384" />
        <path d="M46 68 L74 68 L60 104 Z" fill="#9A9384" fillOpacity="0.45" />
      </g>
      <path d="M84 30 H98 V44" fill="none" stroke="#C9C1AF" strokeWidth="9" strokeLinecap="round" strokeLinejoin="round" strokeDasharray="2 14" />
    </svg>
  );
}

/** Small ring spinner (12px in the page card, 18px in step lists). */
export function Spinner({ size = 12, width = 3 }: { size?: number; width?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 20 20" aria-hidden="true" style={{ flexShrink: 0 }}>
      <circle cx="10" cy="10" r="8" fill="none" stroke="#EAE5D9" strokeWidth={width} />
      <path className="spin" d="M10 2a8 8 0 0 1 8 8" fill="none" stroke="#17160F" strokeWidth={width} strokeLinecap="round" />
    </svg>
  );
}

export const Check = (p: IconProps) => <Stroke stroke={2.25} {...p}><path d="M5 12.5l4.5 4.5L19 7.5" /></Stroke>;
export const Refresh = (p: IconProps) => <Stroke {...p}><path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3M19.5 4.5v4h-4" /></Stroke>;
export const Clock = (p: IconProps) => <Stroke {...p}><circle cx="12" cy="12" r="8.5" /><path d="M12 7.5V12l3 2" /></Stroke>;
export const Sliders = (p: IconProps) => <Stroke {...p}><path d="M4 7h9M17 7h3M4 17h3M11 17h9" /><circle cx="15" cy="7" r="2" /><circle cx="9" cy="17" r="2" /></Stroke>;
export const Mic = (p: IconProps) => <Stroke {...p}><rect x="9" y="3.5" width="6" height="11" rx="3" /><path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v2.5" /></Stroke>;
export const ArrowUp = (p: IconProps) => <Stroke stroke={2} {...p}><path d="M12 19V5M6.5 10.5 12 5l5.5 5.5" /></Stroke>;
export const ArrowRight = (p: IconProps) => <Stroke {...p}><path d="M5 12h14M13 6l6 6-6 6" /></Stroke>;
export const ChevronDown = (p: IconProps) => <Stroke stroke={2} {...p}><path d="M7 10l5 5 5-5" /></Stroke>;
export const ChevronRight = (p: IconProps) => <Stroke {...p}><path d="M10 7l5 5-5 5" /></Stroke>;
export const Hash = (p: IconProps) => <Stroke {...p}><path d="M5 9h14M5 15h14M10.5 4 8.5 20M15.5 4l-2 16" /></Stroke>;
export const Receipt = (p: IconProps) => <Stroke {...p}><path d="M6 3.5h12v17l-3-2-3 2-3-2-3 2z" /><path d="M9.5 8.5h5M9.5 12h5" /></Stroke>;
export const Tag = (p: IconProps) => <Stroke {...p}><path d="M3.5 12.5V4.5a1 1 0 0 1 1-1h8l8 8-9 9z" /><circle cx="8" cy="8" r="1.25" /></Stroke>;
export const Download = (p: IconProps) => <Stroke {...p}><path d="M12 4v11M7 10.5l5 5 5-5M5 20h14" /></Stroke>;
export const Checklist = (p: IconProps) => <Stroke {...p}><path d="M11 6.5h9M11 12h9M11 17.5h9M4 6.5l1.5 1.5L8 5.5M4 12l1.5 1.5L8 11M4 17.5l1.5 1.5L8 16.5" /></Stroke>;
export const LinkIcon = (p: IconProps) => <Stroke {...p}><path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1" /><path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1" /></Stroke>;
export const Lines = (p: IconProps) => <Stroke {...p}><path d="M5 7h14M5 12h14M5 17h8" /></Stroke>;
export const Undo = (p: IconProps) => <Stroke {...p}><path d="M9 14 4.5 9.5 9 5" /><path d="M4.5 9.5H15a5 5 0 0 1 0 10h-3" /></Stroke>;
export const SearchOff = (p: IconProps) => <Stroke {...p}><circle cx="11" cy="11" r="6.5" /><path d="M20 20l-4.2-4.2M8.8 8.8l4.4 4.4M13.2 8.8l-4.4 4.4" /></Stroke>;
export const Lock = (p: IconProps) => <Stroke {...p}><rect x="5" y="11" width="14" height="9.5" rx="2" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></Stroke>;
export const AlertCircle = (p: IconProps) => <Stroke stroke={2} {...p}><circle cx="12" cy="12" r="8.5" /><path d="M12 8v4.5M12 16v.01" /></Stroke>;
export const External = (p: IconProps) => <Stroke stroke={2} {...p}><path d="M14 4.5h5.5V10M19.5 4.5 11 13M18 14v4.5a1 1 0 0 1-1 1H5.5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1H10" /></Stroke>;

/** Icons a suggestion can use, looked up by name from the data. */
export const suggestionIcons = { receipt: Receipt, tag: Tag, download: Download, checklist: Checklist, hash: Hash };
export type SuggestionIcon = keyof typeof suggestionIcons;
