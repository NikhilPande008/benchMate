// Calibration (ARCHITECTURE.md §5.2 step 1, §8): drag the four corners of each display and each
// digit row on the overhead frame; the warped crop and Reader B's reading update while dragging.
// Saving stores the homographies and the reference frame the camera-moved check compares against.

import { useCallback, useEffect, useRef, useState } from 'react';

type Roi = { name: string; corners: [number, number][]; digits: number; kind: string };
type Cal = { camera: string; size: [number, number]; rois: Roi[] };
type Preview = { name: string; image: string | null; reading?: { text: string | null; legible: boolean; conf: number; reason: string | null } };

const LABELS: Record<string, string> = {
  supply: 'Supply display',
  meter: 'Meter display',
  'supply.set_voltage': 'Supply: voltage row',
  'supply.live_current': 'Supply: current row',
  'supply.current_limit': 'Supply: current limit row',
  'meter.main': 'Meter: digits',
};

export function Calibration({ sessionId, onClose }: { sessionId: string; onClose: () => void }) {
  const q = `session=${encodeURIComponent(sessionId)}`;
  const [cal, setCal] = useState<Cal | null>(null);
  const [sel, setSel] = useState<string>('supply.set_voltage');
  const [frameN, setFrameN] = useState(0);
  const [preview, setPreview] = useState<Preview | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const svg = useRef<SVGSVGElement | null>(null);
  const drag = useRef<{ roi: string; i: number } | null>(null);
  const timer = useRef<number | null>(null);

  useEffect(() => {
    fetch(`/screen/calibration?${q}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`calibration: ${r.status}`))))
      .then(setCal)
      .catch((e) => setError(String(e.message ?? e)));
  }, [q]);

  const roi = cal?.rois.find((r) => r.name === sel) ?? null;

  const requestPreview = useCallback(
    (r: Roi) => {
      if (timer.current) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => {
        fetch(`/screen/preview?${q}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: r.name, corners: r.corners, digits: r.digits }),
        })
          .then((x) => (x.ok ? x.json() : null))
          .then((p) => p && setPreview(p))
          .catch(() => {});
      }, 120);
    },
    [q],
  );

  useEffect(() => {
    if (roi) requestPreview(roi);
  }, [sel, cal === null]); // eslint-disable-line react-hooks/exhaustive-deps

  const toSvg = (ev: React.PointerEvent): [number, number] | null => {
    const el = svg.current;
    const m = el?.getScreenCTM();
    if (!el || !m) return null;
    const p = new DOMPoint(ev.clientX, ev.clientY).matrixTransform(m.inverse());
    return [Math.round(p.x * 10) / 10, Math.round(p.y * 10) / 10];
  };

  const move = (ev: React.PointerEvent) => {
    const d = drag.current;
    if (!d || !cal) return;
    const p = toSvg(ev);
    if (!p) return;
    const rois = cal.rois.map((r) => (r.name === d.roi ? { ...r, corners: r.corners.map((c, k) => (k === d.i ? p : c)) as [number, number][] } : r));
    setCal({ ...cal, rois });
    const r = rois.find((x) => x.name === d.roi);
    if (r) requestPreview(r);
    setSaved(null);
  };

  const save = () => {
    if (!cal) return;
    fetch(`/screen/calibration?${q}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ rois: cal.rois.map((r) => ({ name: r.name, corners: r.corners, digits: r.digits })) }),
    })
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`save: ${r.status}`))))
      .then(() => {
        setSaved(new Date().toLocaleTimeString());
        setFrameN((n) => n + 1);
      })
      .catch((e) => setError(String(e.message ?? e)));
  };

  return (
    <div className="modal calibration" role="dialog" data-testid="calibration">
      <div className="panel" onClick={(e) => e.stopPropagation()}>
        <header>
          <h2>Calibrate the overhead camera</h2>
          <button onClick={() => setFrameN((n) => n + 1)}>New frame</button>
          <button onClick={save} data-testid="save-calibration">
            Save
          </button>
          <button onClick={onClose}>Close</button>
        </header>
        {error && <p className="status-word bad">{error}</p>}
        {saved && (
          <p className="status-word ok" data-testid="saved">
            SAVED {saved}
          </p>
        )}
        {cal && (
          <div className="cal-body">
            <ul className="roi-list">
              {cal.rois.map((r) => (
                <li key={r.name}>
                  <button className={r.name === sel ? 'on' : ''} onClick={() => setSel(r.name)} data-testid={`roi-${r.name}`}>
                    {LABELS[r.name] ?? r.name}
                  </button>
                </li>
              ))}
            </ul>
            <div className="cal-frame">
              <img src={`/screen/frame/overhead.jpg?${q}&n=${frameN}`} alt="overhead camera" data-testid="cal-frame" />
              <svg
                ref={svg}
                viewBox={`0 0 ${cal.size[0]} ${cal.size[1]}`}
                className="overlay interactive"
                onPointerMove={move}
                onPointerUp={() => (drag.current = null)}
                data-testid="cal-overlay"
              >
                {cal.rois.map((r) => (
                  <polygon key={r.name} points={r.corners.map((c) => c.join(',')).join(' ')} className={r.name === sel ? 'roi sel' : 'roi'} />
                ))}
                {roi &&
                  roi.corners.map((c, i) => (
                    <circle
                      key={i}
                      cx={c[0]}
                      cy={c[1]}
                      r={9}
                      className="handle"
                      data-testid={`handle-${i}`}
                      onPointerDown={(ev) => {
                        (ev.target as Element).setPointerCapture?.(ev.pointerId);
                        drag.current = { roi: roi.name, i };
                      }}
                    />
                  ))}
              </svg>
            </div>
            <aside className="cal-preview" data-testid="cal-preview">
              <h3>{LABELS[sel] ?? sel}</h3>
              {preview?.name === sel && preview.image && <img src={preview.image} alt="warped crop" data-testid="cal-crop" />}
              {preview?.name === sel && preview.reading && (
                <p data-testid="cal-reading">
                  Reader B: <b>{preview.reading.text ?? '—'}</b>{' '}
                  <span className={`status-word ${preview.reading.legible ? 'ok' : 'warn'}`}>
                    {preview.reading.legible ? `READS ${Math.round(preview.reading.conf * 100)}%` : `UNCLEAR (${preview.reading.reason})`}
                  </span>
                </p>
              )}
              <p className="muted">Drag the four corners tight around the digits (blank positions included).</p>
            </aside>
          </div>
        )}
      </div>
    </div>
  );
}
