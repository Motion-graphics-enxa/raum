import { useEffect, useState } from "react";
import QRCode from "qrcode";

/** QR as an inline SVG image, drawn in the sheet's ink. */
export function QR({ value, size = 168, label }: { value: string; size?: number; label?: string }) {
  const [src, setSrc] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    const ink = getComputedStyle(document.documentElement).getPropertyValue("--ink").trim() || "#0a1d30";
    const paper = getComputedStyle(document.documentElement).getPropertyValue("--paper").trim() || "#ffffff";
    QRCode.toString(value, { type: "svg", margin: 1, errorCorrectionLevel: "M", color: { dark: ink, light: paper } })
      .then((svg) => alive && setSrc(`data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`))
      .catch(() => alive && setSrc(null));
    return () => {
      alive = false;
    };
  }, [value]);
  return src ? (
    <img className="qr" src={src} width={size} height={size} alt={label ?? value} />
  ) : (
    <span className="qr" style={{ width: size, height: size, display: "inline-block" }} />
  );
}
