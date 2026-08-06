"use client";
// Self-contained system-design whiteboard: HTML5 canvas + a small toolbar. No external deps (Excalidraw
// needs network + SSR work in this modified Next 16). Tools: pen, rectangle (component boxes), arrow &
// line (connections), text labels, colors, undo, clear, and export-to-PNG. Pointer events → mouse + touch.
import { useCallback, useEffect, useRef, useState } from "react";
import { Pencil, Square, ArrowUpRight, Minus, Type, Undo2, Trash2, Download } from "lucide-react";

type Pt = { x: number; y: number };
type Shape =
  | { type: "path"; points: Pt[]; color: string; width: number }
  | { type: "rect"; a: Pt; b: Pt; color: string; width: number }
  | { type: "line"; a: Pt; b: Pt; color: string; width: number; arrow: boolean }
  | { type: "text"; at: Pt; text: string; color: string };

type Tool = "pen" | "rect" | "arrow" | "line" | "text";

const COLORS = ["#0f172a", "#2563eb", "#dc2626", "#16a34a", "#d97706"];
const TOOLS: { id: Tool; icon: typeof Pencil; label: string }[] = [
  { id: "pen", icon: Pencil, label: "Pen" },
  { id: "rect", icon: Square, label: "Box" },
  { id: "arrow", icon: ArrowUpRight, label: "Arrow" },
  { id: "line", icon: Minus, label: "Line" },
  { id: "text", icon: Type, label: "Text" },
];

export default function Whiteboard() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const shapesRef = useRef<Shape[]>([]);
  const draftRef = useRef<Shape | null>(null);
  const drawingRef = useRef(false);
  const [tool, setTool] = useState<Tool>("pen");
  const [color, setColor] = useState(COLORS[0]);
  const [text, setText] = useState<{ x: number; y: number; value: string } | null>(null);

  const ratio = () => (typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1);

  const drawShape = (ctx: CanvasRenderingContext2D, s: Shape) => {
    ctx.strokeStyle = s.type === "text" ? s.color : s.color;
    ctx.fillStyle = s.color;
    ctx.lineWidth = "width" in s ? s.width : 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    if (s.type === "path") {
      if (!s.points.length) return;
      ctx.beginPath();
      ctx.moveTo(s.points[0].x, s.points[0].y);
      for (const p of s.points.slice(1)) ctx.lineTo(p.x, p.y);
      ctx.stroke();
    } else if (s.type === "rect") {
      ctx.strokeRect(Math.min(s.a.x, s.b.x), Math.min(s.a.y, s.b.y), Math.abs(s.b.x - s.a.x), Math.abs(s.b.y - s.a.y));
    } else if (s.type === "line") {
      ctx.beginPath();
      ctx.moveTo(s.a.x, s.a.y);
      ctx.lineTo(s.b.x, s.b.y);
      ctx.stroke();
      if (s.arrow) {
        const ang = Math.atan2(s.b.y - s.a.y, s.b.x - s.a.x);
        const h = 12;
        ctx.beginPath();
        ctx.moveTo(s.b.x, s.b.y);
        ctx.lineTo(s.b.x - h * Math.cos(ang - Math.PI / 6), s.b.y - h * Math.sin(ang - Math.PI / 6));
        ctx.moveTo(s.b.x, s.b.y);
        ctx.lineTo(s.b.x - h * Math.cos(ang + Math.PI / 6), s.b.y - h * Math.sin(ang + Math.PI / 6));
        ctx.stroke();
      }
    } else if (s.type === "text") {
      ctx.font = "16px ui-sans-serif, system-ui, sans-serif";
      ctx.textBaseline = "top";
      s.text.split("\n").forEach((ln, i) => ctx.fillText(ln, s.at.x, s.at.y + i * 20));
    }
  };

  const redraw = useCallback(() => {
    const c = canvasRef.current;
    if (!c) return;
    const ctx = c.getContext("2d");
    if (!ctx) return;
    const r = ratio();
    ctx.setTransform(r, 0, 0, r, 0, 0);
    ctx.fillStyle = "#ffffff";
    ctx.fillRect(0, 0, c.width / r, c.height / r);
    const all = draftRef.current ? [...shapesRef.current, draftRef.current] : shapesRef.current;
    for (const s of all) drawShape(ctx, s);
  }, []);

  useEffect(() => {
    const resize = () => {
      const c = canvasRef.current;
      const w = wrapRef.current;
      if (!c || !w) return;
      const r = ratio();
      c.width = Math.max(1, Math.floor(w.clientWidth * r));
      c.height = Math.max(1, Math.floor(w.clientHeight * r));
      c.style.width = w.clientWidth + "px";
      c.style.height = w.clientHeight + "px";
      redraw();
    };
    resize();
    const ro = new ResizeObserver(resize);
    if (wrapRef.current) ro.observe(wrapRef.current);
    return () => ro.disconnect();
  }, [redraw]);

  const pos = (e: React.PointerEvent): Pt => {
    const c = canvasRef.current!;
    const r = c.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  const onDown = (e: React.PointerEvent) => {
    const p = pos(e);
    if (tool === "text") {
      setText({ x: p.x, y: p.y, value: "" });
      return;
    }
    drawingRef.current = true;
    (e.target as Element).setPointerCapture?.(e.pointerId);
    if (tool === "pen") draftRef.current = { type: "path", points: [p], color, width: 2.5 };
    else if (tool === "rect") draftRef.current = { type: "rect", a: p, b: p, color, width: 2 };
    else draftRef.current = { type: "line", a: p, b: { ...p }, color, width: 2, arrow: tool === "arrow" };
    redraw();
  };

  const onMove = (e: React.PointerEvent) => {
    if (!drawingRef.current || !draftRef.current) return;
    const p = pos(e);
    const d = draftRef.current;
    if (d.type === "path") d.points.push(p);
    else if (d.type === "rect") d.b = p;
    else if (d.type === "line") d.b = p;
    redraw();
  };

  const onUp = () => {
    if (draftRef.current) {
      shapesRef.current.push(draftRef.current);
      draftRef.current = null;
    }
    drawingRef.current = false;
    redraw();
  };

  const commitText = () => {
    if (text && text.value.trim())
      shapesRef.current.push({ type: "text", at: { x: text.x, y: text.y }, text: text.value.trim(), color });
    setText(null);
    redraw();
  };

  const undo = () => {
    shapesRef.current.pop();
    redraw();
  };
  const clear = () => {
    shapesRef.current = [];
    redraw();
  };
  const download = () => {
    const c = canvasRef.current;
    if (!c) return;
    const a = document.createElement("a");
    a.download = "system-design.png";
    a.href = c.toDataURL("image/png");
    a.click();
  };

  const btn = (active: boolean) =>
    `p-2 rounded-md border transition ${active ? "bg-emerald-500/30 border-emerald-400 text-emerald-200" : "bg-slate-800 border-slate-700 text-slate-300 hover:bg-slate-700"}`;

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-center gap-1.5 flex-wrap mb-2">
        {TOOLS.map((t) => (
          <button key={t.id} title={t.label} onClick={() => setTool(t.id)} className={btn(tool === t.id)}>
            <t.icon className="w-4 h-4" />
          </button>
        ))}
        <span className="w-px h-6 bg-slate-700 mx-1" />
        {COLORS.map((c) => (
          <button
            key={c}
            title="Color"
            onClick={() => setColor(c)}
            className={`w-6 h-6 rounded-full border-2 ${color === c ? "border-white" : "border-slate-600"}`}
            style={{ backgroundColor: c }}
          />
        ))}
        <span className="w-px h-6 bg-slate-700 mx-1" />
        <button title="Undo" onClick={undo} className={btn(false)}>
          <Undo2 className="w-4 h-4" />
        </button>
        <button title="Clear" onClick={clear} className={btn(false)}>
          <Trash2 className="w-4 h-4" />
        </button>
        <button title="Download PNG" onClick={download} className={btn(false)}>
          <Download className="w-4 h-4" />
        </button>
      </div>
      <div ref={wrapRef} className="relative flex-1 min-h-[360px] rounded-lg overflow-hidden border border-slate-700 bg-white">
        <canvas
          ref={canvasRef}
          onPointerDown={onDown}
          onPointerMove={onMove}
          onPointerUp={onUp}
          onPointerLeave={onUp}
          className="block touch-none"
          style={{ cursor: tool === "text" ? "text" : "crosshair" }}
        />
        {text && (
          <input
            autoFocus
            value={text.value}
            onChange={(e) => setText({ ...text, value: e.target.value })}
            onBlur={commitText}
            onKeyDown={(e) => {
              if (e.key === "Enter") commitText();
              if (e.key === "Escape") setText(null);
            }}
            placeholder="label…"
            className="absolute bg-transparent border-b border-slate-400 outline-none text-[15px]"
            style={{ left: text.x, top: text.y, color }}
          />
        )}
      </div>
    </div>
  );
}
