"""How well does model.text_width() match what KiCad draws?

    "<KiCad>/bin/python.exe" bench/sch/textcheck.py

Writes a scratch sheet with a column of text items (net names, values, references, note lines), renders it with
kicad-cli through the package's renderer, measures the inked width of every line in the PNG and prints it beside
model.text_width(). The layout engine reserves text boxes from text_width (+0.3 .. 0.4 mm), so the number that
matters is the worst UNDER-estimate in millimetres: text wider than its reserved box can touch a neighbour.
Everything is written under bench/sch/.work."""
from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import _env
    _env.setup()

SAMPLES = ["USB_CONN_DP", "QSPI_SCLK", "I2C_SDA", "+3V3", "VBUS", "PWR_FLAG", "100nF", "4.7k", "22uH", "AMS1117-3.3",
           "ESP32-C3-MINI-1-N4", "TS-1187A-B-A-B", "GPIO15", "A1B12", "U12", "STM32G030F6P6TR", "BOOTSEL", "XOUT_R",
           "Place C3, C4 close to U2 +3V3 pins", "D1: I = (3.3 V - Vf 3 V) / 1k = 300 uA", "WWWWMMMM", "iiiillll",
           "0123456789", "LED_K", "nRESET", "SWDIO", "2.54-1*10PPin", "HX PZ2.54-1x5P ZZ"]
X0, Y0, PITCH = 50.0, 20.0, 4.5          # the last line stays above the title block


def measure(work: Path, px_per_mm: float = 20.0, size: float = 1.27) -> list:
    """-> [{'text', 'model_mm', 'kicad_mm', 'error_mm'}] (error > 0: KiCad draws it wider than the model says)."""
    from PIL import Image
    from copper_kicad import render as R
    from copper_kicad.sch.model import Schematic, text_width
    work.mkdir(parents=True, exist_ok=True)
    f = work / "textcheck.kicad_sch"
    sch = Schematic.new(f, "textcheck", paper="A4")
    for i, s in enumerate(SAMPLES):
        sch.add_text(s, (X0, Y0 + i * PITCH), size=size)
    f.write_text(sch.text(), encoding="utf-8", newline="\n")
    (work / "textcheck.kicad_pro").write_text("{}\n", encoding="utf-8")
    imgs = R.schematic(f, work / "png", width=int(297 * px_per_mm), max_sheets=1)
    img = Image.open(imgs[0].path).convert("L")
    ppm = img.size[0] / 297.0
    out = []
    for i, s in enumerate(SAMPLES):
        y = Y0 + i * PITCH
        band = img.crop((int((X0 - 3) * ppm), int((y - 3.0) * ppm), int(250 * ppm), int((y + 1.0) * ppm)))
        bb = band.point(lambda v: 255 if v < 200 else 0).getbbox()
        if bb is None:
            continue
        kic = (bb[2] - bb[0]) / ppm
        out.append({"text": s, "model_mm": round(text_width(s, size), 2), "kicad_mm": round(kic, 2),
                    "error_mm": round(kic - text_width(s, size), 2)})
    return out


def main():
    import _env
    rows = measure(_env.WORK / "textcheck")
    for r in rows:
        print(f"{r['text'][:40]:40s} model {r['model_mm']:6.2f}  kicad {r['kicad_mm']:6.2f}  error {r['error_mm']:+.2f} mm")
    worst = max(r["error_mm"] for r in rows)
    rel = max(abs(r["error_mm"]) / r["kicad_mm"] for r in rows)
    print(f"\n{len(rows)} lines; worst under-estimate {worst:+.2f} mm; largest relative error {rel:.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
