import React from 'react';
import {
  AbsoluteFill,
  Easing,
  interpolate,
  interpolateColors,
  spring,
  useCurrentFrame,
} from 'remotion';
import {loadFont as loadSans} from '@remotion/google-fonts/IBMPlexSans';
import {loadFont as loadMono} from '@remotion/google-fonts/IBMPlexMono';

const sans = loadSans('normal', {weights: ['400', '500', '600'], subsets: ['latin']});
const mono = loadMono('normal', {weights: ['400', '500'], subsets: ['latin']});
const SANS = `${sans.fontFamily}, system-ui, -apple-system, Helvetica, Arial, sans-serif`;
const MONO = `${mono.fontFamily}, ui-monospace, Menlo, monospace`;

export const FPS = 30;
export const TOTAL_FRAMES = 870; // 29.0 s

const C = {
  bg: '#0F141C',
  surface: '#171E29',
  line: '#283243',
  text: '#E7EBF1',
  muted: '#98A2B3',
  accent: '#F08A4B',
  ok: '#6CC495',
  bad: '#F28B82',
  db: '#7AA2F7',
};

// ---------------------------------------------------------------- helpers
const inOut = Easing.bezier(0.45, 0, 0.55, 1);
const outE = Easing.bezier(0.22, 1, 0.36, 1);
const inE = Easing.bezier(0.55, 0, 1, 0.45);

const ramp = (f: number, a: number, b: number, e: (t: number) => number = inOut) =>
  interpolate(f, [a, b], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: e,
  });
const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
const sp = (f: number, start: number, dur = 24) =>
  f <= start
    ? 0
    : spring({frame: f - start, fps: FPS, config: {damping: 200}, durationInFrames: dur});
const mix = (a: string, b: string, t: number) =>
  interpolateColors(Math.min(1, Math.max(0, t)), [0, 1], [a, b]);

type R = {x: number; y: number; w: number; h: number};
const lerpR = (a: R, b: R, t: number): R => ({
  x: lerp(a.x, b.x, t),
  y: lerp(a.y, b.y, t),
  w: lerp(a.w, b.w, t),
  h: lerp(a.h, b.h, t),
});

// Orthogonal polyline with rounded corners.
const ortho = (pts: number[][], r = 7) => {
  let d = `M ${pts[0][0]} ${pts[0][1]}`;
  for (let i = 1; i < pts.length - 1; i++) {
    const [x0, y0] = pts[i - 1];
    const [x1, y1] = pts[i];
    const [x2, y2] = pts[i + 1];
    const d1 = Math.hypot(x1 - x0, y1 - y0);
    const d2 = Math.hypot(x2 - x1, y2 - y1);
    const rr = Math.min(r, d1 / 2, d2 / 2);
    const ax = x1 - ((x1 - x0) / d1) * rr;
    const ay = y1 - ((y1 - y0) / d1) * rr;
    const bx = x1 + ((x2 - x1) / d2) * rr;
    const by = y1 + ((y2 - y1) / d2) * rr;
    d += ` L ${ax} ${ay} Q ${x1} ${y1} ${bx} ${by}`;
  }
  const last = pts[pts.length - 1];
  d += ` L ${last[0]} ${last[1]}`;
  return d;
};

// ---------------------------------------------------------------- primitives
type BoxProps = {
  x: number;
  y: number;
  w: number;
  h: number;
  fill?: string;
  stroke?: string;
  sw?: number;
  r?: number;
  dashed?: boolean;
  dash?: number;
  strokeOpacity?: number;
  opacity?: number;
  style?: React.CSSProperties;
  children?: React.ReactNode;
};

const Box: React.FC<BoxProps> = ({
  x,
  y,
  w,
  h,
  fill = 'none',
  stroke = C.line,
  sw = 1.5,
  r = 12,
  dashed = false,
  dash = 7,
  strokeOpacity = 1,
  opacity = 1,
  style,
  children,
}) => (
  <div style={{position: 'absolute', left: x, top: y, width: w, height: h, opacity, ...style}}>
    <svg
      width={w}
      height={h}
      style={{position: 'absolute', left: 0, top: 0, overflow: 'visible'}}
    >
      <rect
        x={sw / 2}
        y={sw / 2}
        width={Math.max(0, w - sw)}
        height={Math.max(0, h - sw)}
        rx={Math.max(0, r - sw / 2)}
        fill={fill}
        stroke={stroke}
        strokeWidth={sw}
        strokeOpacity={strokeOpacity}
        strokeDasharray={dashed ? `${dash} ${dash * 0.72}` : undefined}
      />
    </svg>
    {children}
  </div>
);

const Center: React.FC<{children: React.ReactNode; style?: React.CSSProperties}> = ({
  children,
  style,
}) => (
  <div
    style={{
      position: 'absolute',
      inset: 0,
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      whiteSpace: 'nowrap',
      lineHeight: 1,
      ...style,
    }}
  >
    {children}
  </div>
);

const T: React.FC<{
  x: number;
  y: number;
  size: number;
  font?: string;
  color?: string;
  weight?: number;
  opacity?: number;
  align?: 'left' | 'right';
  children: React.ReactNode;
}> = ({x, y, size, font = SANS, color = C.text, weight = 400, opacity = 1, align = 'left', children}) => (
  <div
    style={{
      position: 'absolute',
      left: align === 'left' ? x : undefined,
      right: align === 'right' ? x : undefined,
      top: y,
      fontFamily: font,
      fontSize: size,
      color,
      fontWeight: weight,
      lineHeight: 1.2,
      whiteSpace: 'nowrap',
      opacity,
    }}
  >
    {children}
  </div>
);

const Caption: React.FC<{f: number; a: number; b: number; text: string}> = ({f, a, b, text}) => {
  const o = Math.min(ramp(f, a, a + 12), 1 - ramp(f, b - 10, b));
  if (o <= 0) return null;
  const dy = lerp(8, 0, ramp(f, a, a + 16, outE));
  return (
    <div
      style={{
        position: 'absolute',
        left: 48,
        right: 48,
        top: 620,
        height: 44,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        fontFamily: SANS,
        fontSize: 32,
        color: C.text,
        whiteSpace: 'nowrap',
        lineHeight: 1,
        opacity: o,
        transform: `translateY(${dy}px)`,
      }}
    >
      {text}
    </div>
  );
};

// ---------------------------------------------------------------- scene 0 · the problem (0–135)
const Scene0: React.FC<{f: number}> = ({f}) => {
  if (f >= 135) return null;
  const inV = sp(f, 0, 20);
  const red = ramp(f, 66, 78);
  const out = ramp(f, 100, 118);
  const bx = 420;
  const by = 170;
  const bw = 440;
  const bh = 300;
  const scale = lerp(0.97, 1, inV) * lerp(1, 0.98, out);
  const item = (i: number) => {
    const a = sp(f, 8 + i * 6, 18);
    const o = ramp(f, 80 + i * 5, 94 + i * 5);
    return {
      opacity: a * (1 - o),
      transform: `translateY(${lerp(8, 0, a) + 6 * o}px)`,
    };
  };
  return (
    <AbsoluteFill style={{opacity: inV * (1 - out), transform: `scale(${scale})`}}>
      <Box
        x={bx}
        y={by}
        w={bw}
        h={bh}
        fill={C.surface}
        stroke={mix(C.line, C.bad, red)}
        sw={lerp(1.5, 2, red)}
      >
        <div style={item(0)}>
          <T x={20} y={16} size={16} font={MONO} color={C.muted}>
            sandbox
          </T>
        </div>
        {/* agent process chip */}
        <div style={{position: 'absolute', inset: 0, ...item(0)}}>
          <Box x={(bw - 132) / 2} y={78} w={132} h={40} fill={C.bg} r={10}>
            <Center style={{gap: 10, fontFamily: SANS, fontSize: 18, color: C.text}}>
              <div style={{width: 8, height: 8, borderRadius: 4, background: C.accent}} />
              agent
            </Center>
          </Box>
        </div>
        {/* files */}
        <div style={{position: 'absolute', inset: 0, ...item(1)}}>
          {['notes.md', 'app.py'].map((name, i) => (
            <Box key={name} x={72 + i * 156} y={138} w={140} h={44} fill={C.bg} r={10}>
              <Center style={{fontFamily: MONO, fontSize: 15, color: C.text}}>{name}</Center>
            </Box>
          ))}
        </div>
        {/* conversation history */}
        <div style={{position: 'absolute', inset: 0, ...item(2)}}>
          <Box x={(bw - 250) / 2} y={202} w={250} h={40} fill={C.bg} r={10}>
            <Center style={{fontFamily: SANS, fontSize: 17, color: C.text}}>
              conversation history
            </Center>
          </Box>
        </div>
      </Box>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- scene 1 · title (135–210)
const Scene1: React.FC<{f: number}> = ({f}) => {
  if (f < 135 || f >= 210) return null;
  const t = ramp(f, 138, 156) * (1 - ramp(f, 194, 208));
  const s = ramp(f, 146, 164) * (1 - ramp(f, 194, 208));
  return (
    <AbsoluteFill
      style={{alignItems: 'center', justifyContent: 'center', flexDirection: 'column', gap: 18}}
    >
      <div
        style={{
          fontFamily: SANS,
          fontSize: 96,
          fontWeight: 600,
          color: C.text,
          letterSpacing: -1.5,
          lineHeight: 1,
          opacity: t,
          transform: `translateY(${lerp(12, 0, sp(f, 138, 26))}px)`,
        }}
      >
        roost
      </div>
      <div
        style={{
          fontFamily: SANS,
          fontSize: 30,
          color: C.muted,
          lineHeight: 1.2,
          opacity: s,
          transform: `translateY(${lerp(10, 0, sp(f, 146, 26))}px)`,
        }}
      >
        A durable runtime for any agent SDK.
      </div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- scene 3 layout geometry (world)
const W: R = {x: 120, y: 140, w: 1040, h: 292};
const SB: R = {x: 120, y: 456, w: 1040, h: 56};
const FILES = ['notes.md', 'app.py', 'data.csv'];
const FILE_CX = [471, 639.5, 808];
const FILE_W = 150;
const FILE_Y = 192;
const FILE_H = 44;
const CARD_X = [144, 481, 818];
const CARD_Y = 264;
const CARD_W = 317;
const CARD_H = 144;
const CHIP_Y = 76; // relative to card
const CHIP_H = 46;
const INBOX = {x: 18, w: 118};
const SESSION = {x: 148, w: 151};
const DOTS = [1, 0, 2];

// scene 2 geometry
const PANEL_X = [90, 470, 850];
const PANEL_Y = 70;
const PANEL_W = 340;
const PANEL_H = 490;
const TENANTS = [
  {name: 'acme', users: ['alice', 'bob']},
  {name: 'globex', users: ['carol', 'dan']},
  {name: 'initech', users: ['erin']},
];
const TILE = {dx: 60, dy: 84, w: 220, h: 110, step: 204};
const CHIP = {dx: 110, dy: 138, w: 120, h: 30}; // dy relative to tile top

// camera for the tenants → alice zoom
const S_FINAL = 3.5;
const camera = (f: number) => {
  const uA = ramp(f, 276, 312);
  const uB = ramp(f, 303, 345);
  const Fx = lerp(640, 260, uA);
  const Fy = 315 + (209 - 315) * uB;
  const Cx = 640;
  const Cy = lerp(315, W.y + W.h / 2, uB);
  const logS = Math.log(1.08) * uA + (Math.log(S_FINAL) - Math.log(1.08)) * uB;
  const s = Math.exp(logS);
  return {s, tx: Cx - Fx * s, ty: Cy - Fy * s, uA, uB};
};

// ---------------------------------------------------------------- scene 2 · tenants (210–345)
const Scene2: React.FC<{f: number}> = ({f}) => {
  if (f < 210 || f >= 345) return null;
  const cam = camera(f);
  const acmeFade = 1 - ramp(f, 302, 320);
  const panelIn = (i: number) => sp(f, 212 + i * 6, 22);
  const groupIn = (k: number) => sp(f, 226 + k * 5, 20);
  let k = 0;
  const groups: {p: number; u: number; k: number}[] = [];
  TENANTS.forEach((t, p) =>
    t.users.forEach((_, u) => {
      groups.push({p, u, k});
      k++;
    }),
  );

  // alice overlay (screen space) morphing into the workspace box
  const aIn = groupIn(0);
  const pOff = lerp(16, 0, panelIn(0)) + lerp(10, 0, aIn);
  const worldTile: R = {
    x: PANEL_X[0] + TILE.dx,
    y: PANEL_Y + TILE.dy + pOff,
    w: TILE.w,
    h: TILE.h,
  };
  const camRect = (r: R): R => ({
    x: r.x * cam.s + cam.tx,
    y: r.y * cam.s + cam.ty,
    w: r.w * cam.s,
    h: r.h * cam.s,
  });
  const m = ramp(f, 316, 345);
  const tile = lerpR(camRect(worldTile), W, m);
  const worldChip: R = {
    x: PANEL_X[0] + CHIP.dx,
    y: PANEL_Y + TILE.dy + CHIP.dy + pOff,
    w: CHIP.w,
    h: CHIP.h,
  };
  const mc = ramp(f, 314, 345);
  const chip = lerpR(camRect(worldChip), SB, mc);
  const chipFont = lerp(14 * cam.s, 16, mc);
  const chipTextW = chipFont * 0.6 * 7;
  const chipTextX = lerp((chip.w - chipTextW) / 2, 20, mc);

  return (
    <AbsoluteFill>
      {/* world */}
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: 0,
          width: 1280,
          height: 720,
          transformOrigin: '0 0',
          transform: `translate(${cam.tx}px, ${cam.ty}px) scale(${cam.s})`,
        }}
      >
        {TENANTS.map((t, p) => {
          const a = panelIn(p);
          const out = p === 0 ? 0 : cam.uA;
          const slide = p === 0 ? 0 : 150 * out * (p === 1 ? 1 : 1.3);
          const op = a * (p === 0 ? acmeFade : 1 - ramp(f, 278, 304));
          return (
            <div
              key={t.name}
              style={{
                position: 'absolute',
                inset: 0,
                opacity: op,
                transformOrigin: `${PANEL_X[p] + PANEL_W / 2}px ${PANEL_Y + PANEL_H / 2}px`,
                transform: `translate(${slide}px, ${lerp(16, 0, a)}px) scale(${1 - 0.06 * out})`,
              }}
            >
              <Box
                x={PANEL_X[p]}
                y={PANEL_Y}
                w={PANEL_W}
                h={PANEL_H}
                fill={C.surface}
                stroke={C.muted}
                strokeOpacity={0.5}
                sw={3}
                r={14}
              >
                <T x={24} y={20} size={18} font={MONO} color={C.muted}>
                  tenant · {t.name}
                </T>
              </Box>
            </div>
          );
        })}
        {groups.map(({p, u, k: gk}) => {
          const isAlice = gk === 0;
          const a = groupIn(gk);
          const panelA = panelIn(p);
          const out = p === 0 ? 0 : cam.uA;
          const slide = p === 0 ? 0 : 150 * out * (p === 1 ? 1 : 1.3);
          const op = a * panelA * (p === 0 ? acmeFade : 1 - ramp(f, 278, 304));
          const gx = PANEL_X[p];
          const gy = PANEL_Y + TILE.dy + u * TILE.step;
          const name = TENANTS[p].users[u];
          return (
            <div
              key={name}
              style={{
                position: 'absolute',
                inset: 0,
                opacity: op,
                transformOrigin: `${PANEL_X[p] + PANEL_W / 2}px ${PANEL_Y + PANEL_H / 2}px`,
                transform: `translate(${slide}px, ${lerp(16, 0, panelA) + lerp(10, 0, a)}px) scale(${
                  1 - 0.06 * out
                })`,
              }}
            >
              {!isAlice && (
                <Box
                  x={gx + TILE.dx}
                  y={gy}
                  w={TILE.w}
                  h={TILE.h}
                  fill={C.bg}
                  stroke={C.muted}
                  r={12}
                >
                  <Center style={{fontFamily: SANS, fontSize: 22, color: C.text}}>{name}</Center>
                </Box>
              )}
              {/* stand / base line */}
              <div
                style={{
                  position: 'absolute',
                  left: gx + 120,
                  top: gy + TILE.h + 10,
                  width: 100,
                  height: 2,
                  borderRadius: 1,
                  background: C.muted,
                }}
              />
              {!isAlice && (
                <Box
                  x={gx + CHIP.dx}
                  y={gy + CHIP.dy}
                  w={CHIP.w}
                  h={CHIP.h}
                  stroke={C.muted}
                  dashed
                  dash={5}
                  r={8}
                >
                  <Center style={{fontFamily: MONO, fontSize: 14, color: C.muted}}>sandbox</Center>
                </Box>
              )}
            </div>
          );
        })}
      </div>

      {/* alice tile → workspace box */}
      <div style={{position: 'absolute', inset: 0, opacity: aIn * panelIn(0)}}>
        <Box
          x={tile.x}
          y={tile.y}
          w={tile.w}
          h={tile.h}
          fill={mix(C.bg, C.surface, m)}
          stroke={mix(C.muted, C.line, m)}
          sw={lerp(1.5 * cam.s, 1.5, m)}
          r={lerp(12 * cam.s, 12, m)}
        >
          <Center
            style={{
              fontFamily: SANS,
              fontSize: 22 * cam.s,
              color: C.text,
              opacity: 1 - ramp(f, 310, 326),
            }}
          >
            alice
          </Center>
          <T x={24} y={16} size={16} font={MONO} color={C.muted} opacity={ramp(f, 330, 345)}>
            workspace · alice
          </T>
        </Box>
        <Box
          x={chip.x}
          y={chip.y}
          w={chip.w}
          h={chip.h}
          stroke={C.muted}
          sw={lerp(1.5 * cam.s, 1.5, mc)}
          dashed
          dash={lerp(5 * cam.s, 7, mc)}
          r={lerp(8 * cam.s, 12, mc)}
        >
          <div
            style={{
              position: 'absolute',
              left: chipTextX,
              top: 0,
              height: chip.h,
              display: 'flex',
              alignItems: 'center',
              fontFamily: MONO,
              fontSize: chipFont,
              color: C.muted,
              lineHeight: 1,
              whiteSpace: 'nowrap',
            }}
          >
            sandbox
          </div>
        </Box>
      </div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- workspace layout (scenes 3–5)
const DEATH = 670; // sandbox dies (bar freezes)
const barProgress = (f: number) => {
  const start = 380;
  const frozenAt = 0.62;
  if (f < start) return 0;
  if (f <= DEATH) return ((f - start) / (DEATH - start)) * frozenAt;
  return lerp(frozenAt, 1, ramp(f, 736, 756));
};

// scene 4: colour-code items by the store that holds their state (kept through scene 5)
const TINT_DB = 524; // (a) inboxes, conversation cards, turn bar → database
const TINT_SN = 535; // (b) files, agent sessions → snapshots
const TINT_SB = 545; // (c) sandbox block → "processes · memory"
const tintAt = (f: number, start: number, i = 0) => ramp(f, start + i * 2, start + i * 2 + 12);

const TintDot: React.FC<{w: number; color: string; t: number; inset?: number}> = ({
  w,
  color,
  t,
  inset = 9,
}) =>
  t > 0 ? (
    <div
      style={{
        position: 'absolute',
        left: w - inset - 3,
        top: inset - 3,
        width: 6,
        height: 6,
        borderRadius: 3,
        background: color,
        opacity: t,
      }}
    />
  ) : null;

const SandboxBlock: React.FC<{
  stroke: string;
  sw?: number;
  note: number;
  opacity?: number;
  style?: React.CSSProperties;
}> = ({stroke, sw = 1.5, note, opacity = 1, style}) => (
  <Box
    x={SB.x}
    y={SB.y}
    w={SB.w}
    h={SB.h}
    stroke={stroke}
    sw={sw}
    dashed
    opacity={opacity}
    style={style}
  >
    <div
      style={{
        position: 'absolute',
        left: 20,
        right: 20,
        top: 0,
        height: SB.h,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'space-between',
        lineHeight: 1,
        whiteSpace: 'nowrap',
      }}
    >
      <span style={{fontFamily: MONO, fontSize: 16, color: stroke}}>sandbox</span>
      {/* bg knockout: the scene-5 restore lines pass behind this label */}
      <span
        style={{
          fontFamily: SANS,
          fontSize: 15,
          color: stroke,
          opacity: note,
          background: C.bg,
          padding: '2px 0 2px 8px',
        }}
      >
        processes · memory
      </span>
    </div>
  </Box>
);

const Workspace: React.FC<{f: number}> = ({f}) => {
  // scene 5 events
  const red = ramp(f, DEATH, DEATH + 9);
  const drop = ramp(f, 680, 696, inE);
  const newIn = sp(f, 690, 22);
  const restoreFiles = ramp(f, 706, 724, outE);
  const restoreSess = ramp(f, 709, 727, outE);
  const restoreFade = 1 - ramp(f, 738, 750);
  const filesHit = ramp(f, 722, 728) * (1 - ramp(f, 734, 748));
  const sessHit = ramp(f, 725, 731) * (1 - ramp(f, 737, 751));

  // thread 2: running (accent) → database tint; the bar then completes in blue
  const acc = ramp(f, 376, 388);
  const bar = barProgress(f);
  const barColor = mix(C.accent, C.db, tintAt(f, TINT_DB, 1));
  const sbNote = tintAt(f, TINT_SB);

  const restorePaths: {d: string; p: number}[] = [
    {d: ortho([[FILE_CX[0], 600], [FILE_CX[0], FILE_Y + FILE_H]]), p: restoreFiles},
    {d: ortho([[FILE_CX[2], 600], [FILE_CX[2], FILE_Y + FILE_H]]), p: restoreFiles},
    {
      d: ortho([
        [FILE_CX[0], 600],
        [FILE_CX[0], 250],
        [FILE_CX[1], 250],
        [FILE_CX[1], FILE_Y + FILE_H],
      ]),
      p: restoreFiles,
    },
    ...CARD_X.map((cx) => ({
      d: ortho([
        [cx + SESSION.x + SESSION.w / 2, 600],
        [cx + SESSION.x + SESSION.w / 2, CARD_Y + CHIP_Y + CHIP_H],
      ]),
      p: restoreSess,
    })),
  ];

  return (
    <>
      <Box x={W.x} y={W.y} w={W.w} h={W.h} fill={C.surface}>
        <T x={24} y={16} size={16} font={MONO} color={C.muted}>
          workspace · alice
        </T>
      </Box>
      {FILES.map((name, i) => {
        const a = sp(f, 350 + i * 4, 18);
        const sn = tintAt(f, TINT_SN, i);
        return (
          <Box
            key={name}
            x={FILE_CX[i] - FILE_W / 2}
            y={FILE_Y}
            w={FILE_W}
            h={FILE_H}
            fill={C.bg}
            stroke={mix(C.line, C.ok, sn)}
            sw={1.5 + filesHit}
            r={10}
            opacity={a}
            style={{transform: `translateY(${lerp(8, 0, a)}px)`}}
          >
            <Center style={{fontFamily: MONO, fontSize: 15, color: C.text}}>{name}</Center>
            <TintDot w={FILE_W} color={C.ok} t={sn} />
          </Box>
        );
      })}
      {CARD_X.map((cx, i) => {
        const a = sp(f, 358 + i * 6, 20);
        const db = tintAt(f, TINT_DB, i);
        const sn = tintAt(f, TINT_SN, i + 1);
        const base = i === 1 ? mix(C.line, C.accent, acc) : C.line;
        return (
          <Box
            key={cx}
            x={cx}
            y={CARD_Y}
            w={CARD_W}
            h={CARD_H}
            fill={C.bg}
            stroke={mix(base, C.db, db)}
            opacity={a}
            style={{transform: `translateY(${lerp(10, 0, a)}px)`}}
          >
            <T x={18} y={16} size={15} font={MONO} color={C.muted}>
              conversation · thread {i + 1}
            </T>
            <TintDot w={CARD_W} color={C.db} t={db} inset={14} />
            {i === 1 && (
              <div style={{opacity: ramp(f, 376, 388)}}>
                <div
                  style={{
                    position: 'absolute',
                    left: 18,
                    top: 52,
                    width: CARD_W - 36,
                    height: 6,
                    borderRadius: 3,
                    background: C.line,
                  }}
                />
                <div
                  style={{
                    position: 'absolute',
                    left: 18,
                    top: 52,
                    width: (CARD_W - 36) * bar,
                    height: 6,
                    borderRadius: 3,
                    background: barColor,
                  }}
                />
              </div>
            )}
            <Box
              x={INBOX.x}
              y={CHIP_Y}
              w={INBOX.w}
              h={CHIP_H}
              fill={C.surface}
              stroke={mix(C.line, C.db, db)}
              r={10}
            >
              <div
                style={{
                  position: 'absolute',
                  left: 14,
                  top: 0,
                  height: CHIP_H,
                  display: 'flex',
                  alignItems: 'center',
                  fontFamily: SANS,
                  fontSize: 17,
                  color: C.text,
                  lineHeight: 1,
                }}
              >
                inbox
              </div>
              {Array.from({length: DOTS[i]}).map((_, d) => (
                <div
                  key={d}
                  style={{
                    position: 'absolute',
                    width: 9,
                    height: 9,
                    borderRadius: 4.5,
                    background: C.muted,
                    top: (CHIP_H - 9) / 2,
                    left: INBOX.w - 22 - 9 - d * 15,
                  }}
                />
              ))}
              <TintDot w={INBOX.w} color={C.db} t={db} />
            </Box>
            <Box
              x={SESSION.x}
              y={CHIP_Y}
              w={SESSION.w}
              h={CHIP_H}
              fill={C.surface}
              stroke={mix(C.line, C.ok, sn)}
              sw={1.5 + sessHit}
              r={10}
            >
              <Center style={{fontFamily: SANS, fontSize: 17, color: C.text}}>agent session</Center>
              <TintDot w={SESSION.w} color={C.ok} t={sn} />
            </Box>
          </Box>
        );
      })}
      {/* restore from snapshots: green lines into the green items only */}
      {restoreFiles > 0 && restoreFade > 0 && (
        <svg
          width={1280}
          height={720}
          style={{position: 'absolute', left: 0, top: 0, opacity: restoreFade}}
        >
          {restorePaths.map((rp, i) =>
            rp.p > 0 ? (
              <path
                key={i}
                d={rp.d}
                fill="none"
                stroke={C.ok}
                strokeWidth={1.5}
                strokeLinecap="round"
                strokeLinejoin="round"
                pathLength={1}
                strokeDasharray="1 1"
                strokeDashoffset={1 - rp.p}
              />
            ) : null,
          )}
        </svg>
      )}
      {/* sandbox machine block (original) */}
      {drop < 1 && (
        <SandboxBlock
          stroke={mix(C.muted, C.bad, red)}
          sw={lerp(1.5, 2, red)}
          note={sbNote}
          opacity={1 - drop}
          style={{transform: `translateY(${40 * drop}px)`}}
        />
      )}
      {/* replacement sandbox */}
      {newIn > 0 && (
        <SandboxBlock
          stroke={C.muted}
          note={sbNote}
          opacity={newIn}
          style={{transform: `translateX(${lerp(140, 0, newIn)}px)`}}
        />
      )}
    </>
  );
};

// ---------------------------------------------------------------- scene 4 · legend (screen space)
const S4 = 0.9; // layout scale while the legend is up
const S4_TOP = 56; // screen y of the workspace box top while shrunk
const LEGEND_Y = 444;
const LEGEND_W = 340;
const LEGEND_H = 108;
const LEGEND_X = [98, 470, 842];

const Legend: React.FC<{f: number}> = ({f}) => {
  if (f < 560 || f >= 660) return null;
  const fadeOut = 1 - ramp(f, 645, 657);
  const items = [
    {name: 'database', line: 'conversations · inboxes · turns', color: C.db, dashed: false},
    {name: 'snapshots', line: 'files · agent sessions', color: C.ok, dashed: false},
    {name: 'sandbox', line: 'processes · memory', color: C.muted, dashed: true},
  ];
  const checks = [ramp(f, 604, 618, outE), ramp(f, 610, 624, outE), 0];
  const disposable = ramp(f, 614, 628);

  return (
    <AbsoluteFill style={{opacity: fadeOut}}>
      {items.map((it, i) => {
        const a = sp(f, 560 + i * 11, 20);
        return (
          <Box
            key={it.name}
            x={LEGEND_X[i]}
            y={LEGEND_Y}
            w={LEGEND_W}
            h={LEGEND_H}
            fill={it.dashed ? C.bg : C.surface}
            stroke={it.dashed ? C.muted : C.line}
            dashed={it.dashed}
            opacity={a}
            style={{transform: `translateY(${lerp(14, 0, a)}px)`}}
          >
            <div
              style={{
                position: 'absolute',
                left: 20,
                top: 22,
                width: 12,
                height: 12,
                borderRadius: 3,
                boxSizing: 'border-box',
                background: it.dashed ? 'transparent' : it.color,
                border: it.dashed ? `1.5px solid ${it.color}` : undefined,
              }}
            />
            <T x={42} y={16} size={19} font={MONO} color={it.dashed ? C.muted : C.text}>
              {it.name}
            </T>
            <T x={20} y={50} size={15} color={C.muted}>
              {it.line}
            </T>
            {i === 1 && (
              <T x={20} y={76} size={12.5} font={MONO} color={C.muted}>
                tenants/acme/workspaces/alice
              </T>
            )}
            {checks[i] > 0 && (
              <svg
                width={22}
                height={18}
                viewBox="0 0 22 18"
                style={{position: 'absolute', right: 20, top: 20, overflow: 'visible'}}
              >
                <path
                  d="M 2 9.5 L 8 15.5 L 20 2.5"
                  fill="none"
                  stroke={C.ok}
                  strokeWidth={2.5}
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  pathLength={1}
                  strokeDasharray="1 1"
                  strokeDashoffset={1 - checks[i]}
                />
              </svg>
            )}
            {i === 2 && (
              <T x={20} y={19} size={15} color={C.muted} align="right" opacity={disposable}>
                disposable
              </T>
            )}
          </Box>
        );
      })}
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- scene 6 · end card (780–870)
const Scene6: React.FC<{f: number}> = ({f}) => {
  if (f < 780) return null;
  // all three lines are fully in by frame 806 (hold 806–870)
  const a = ramp(f, 784, 798);
  const b = ramp(f, 788, 802);
  const c = ramp(f, 792, 806);
  return (
    <AbsoluteFill
      style={{alignItems: 'center', justifyContent: 'center', flexDirection: 'column'}}
    >
      <div
        style={{
          fontFamily: SANS,
          fontSize: 48,
          fontWeight: 500,
          color: C.text,
          letterSpacing: -0.5,
          lineHeight: 1.1,
          whiteSpace: 'nowrap',
          opacity: a,
          transform: `translateY(${lerp(10, 0, sp(f, 784, 26))}px)`,
        }}
      >
        Build all your enterprise agents on one stack.
      </div>
      <div
        style={{
          marginTop: 18,
          fontFamily: SANS,
          fontSize: 24,
          color: C.muted,
          lineHeight: 1.2,
          whiteSpace: 'nowrap',
          opacity: b,
          transform: `translateY(${lerp(8, 0, sp(f, 788, 26))}px)`,
        }}
      >
        Agent state lives in your own S3, private and out of the sandbox.
      </div>
      <div
        style={{
          marginTop: 34,
          fontFamily: MONO,
          fontSize: 24,
          color: C.muted,
          lineHeight: 1.2,
          whiteSpace: 'nowrap',
          opacity: c,
          transform: `translateY(${lerp(8, 0, sp(f, 792, 26))}px)`,
        }}
      >
        github.com/JerryChaox/roost
      </div>
    </AbsoluteFill>
  );
};

// ---------------------------------------------------------------- composition
export const Teaser: React.FC = () => {
  const f = useCurrentFrame();

  const shrink = sp(f, 495, 26) - sp(f, 646, 24);
  const ls = lerp(1, S4, shrink);
  const lty = lerp(0, S4_TOP - W.y, shrink);
  const layoutOpacity = 1 - ramp(f, 770, 782);
  const showLayout = f >= 345 && f < 782;

  return (
    <AbsoluteFill style={{backgroundColor: C.bg}}>
      <Scene0 f={f} />
      <Scene1 f={f} />
      <Scene2 f={f} />
      {showLayout && (
        <div
          style={{
            position: 'absolute',
            left: 0,
            top: 0,
            width: 1280,
            height: 720,
            opacity: layoutOpacity,
            transformOrigin: `640px ${W.y}px`,
            transform: `translate(0px, ${lty}px) scale(${ls})`,
          }}
        >
          <Workspace f={f} />
        </div>
      )}
      <Legend f={f} />
      <Scene6 f={f} />

      <Caption f={f} a={9} b={70} text="Your agent lives in a sandbox." />
      <Caption f={f} a={72} b={135} text="When the sandbox dies, the agent forgets everything." />
      <Caption f={f} a={220} b={334} text="Every tenant is walled off. Every user gets a workspace." />
      <Caption
        f={f}
        a={352}
        b={495}
        text="Conversations share files. Each keeps its own session and inbox."
      />
      <Caption f={f} a={500} b={645} text="Nothing that matters lives in the sandbox." />
      <Caption f={f} a={650} b={772} text="The sandbox can die. The agent carries on." />
    </AbsoluteFill>
  );
};
