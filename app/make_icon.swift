// Рисует иконку приложения (1024×1024 PNG): запускается из build_app.sh через `swift`.
// Использование: swift app/make_icon.swift <out.png>

import AppKit

let size: CGFloat = 1024
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "icon.png"
let img = NSImage(size: NSSize(width: size, height: size))
img.lockFocus()

// Плашка в стиле macOS: скруглённый квадрат с отступом и градиентом.
let inset: CGFloat = 100
let rect = NSRect(x: inset, y: inset, width: size - 2 * inset, height: size - 2 * inset)
let path = NSBezierPath(roundedRect: rect, xRadius: 185, yRadius: 185)
NSGraphicsContext.current?.saveGraphicsState()
let shadow = NSShadow()
shadow.shadowColor = NSColor.black.withAlphaComponent(0.3)
shadow.shadowBlurRadius = 24
shadow.shadowOffset = NSSize(width: 0, height: -10)
shadow.set()
NSGradient(starting: NSColor(calibratedRed: 0.20, green: 0.45, blue: 0.95, alpha: 1),
           ending: NSColor(calibratedRed: 0.36, green: 0.22, blue: 0.80, alpha: 1))!.draw(in: path, angle: -60)
NSGraphicsContext.current?.restoreGraphicsState()

// Звуковая волна из столбиков.
NSColor.white.setFill()
let heights: [CGFloat] = [0.22, 0.42, 0.68, 0.48, 0.86, 0.56, 0.34, 0.62, 0.40, 0.20]
let barW: CGFloat = 44, gap: CGFloat = 22
let total = CGFloat(heights.count) * barW + CGFloat(heights.count - 1) * gap
var x = (size - total) / 2
for h in heights {
    let barH = h * (rect.height * 0.62)
    let bar = NSRect(x: x, y: size / 2 - barH / 2, width: barW, height: barH)
    NSBezierPath(roundedRect: bar, xRadius: barW / 2, yRadius: barW / 2).fill()
    x += barW + gap
}
img.unlockFocus()

guard let tiff = img.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
      let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
try! png.write(to: URL(fileURLWithPath: out))
