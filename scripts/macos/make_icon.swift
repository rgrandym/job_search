// Draws the Dock app's icon (a briefcase on a rounded square) as a 1024 px PNG.
// Usage: make_icon <out.png>. Run by make_app.sh, which turns it into AppIcon.icns.

import AppKit

let size = 1024.0
guard CommandLine.arguments.count == 2,
    let rep = NSBitmapImageRep(
        bitmapDataPlanes: nil, pixelsWide: Int(size), pixelsHigh: Int(size), bitsPerSample: 8,
        samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
        bytesPerRow: 0, bitsPerPixel: 0)
else {
    FileHandle.standardError.write("usage: make_icon <out.png>\n".data(using: .utf8)!)
    exit(2)
}

NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
// macOS icon grid: an 824 px rounded square centred on the 1024 px canvas.
let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
let shape = NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185)
NSGradient(
    starting: NSColor(srgbRed: 0.25, green: 0.36, blue: 0.85, alpha: 1),
    ending: NSColor(srgbRed: 0.10, green: 0.62, blue: 0.70, alpha: 1)
)!.draw(in: shape, angle: -90)

let symbol = NSImage.SymbolConfiguration(pointSize: 420, weight: .semibold)
    .applying(NSImage.SymbolConfiguration(paletteColors: [.white]))
if let briefcase = NSImage(systemSymbolName: "briefcase.fill", accessibilityDescription: nil)?
    .withSymbolConfiguration(symbol)
{
    let box = briefcase.size
    briefcase.draw(in: NSRect(
        x: (size - box.width) / 2, y: (size - box.height) / 2, width: box.width, height: box.height))
}
NSGraphicsContext.current = nil

let url = URL(fileURLWithPath: CommandLine.arguments[1])
try rep.representation(using: .png, properties: [:])!.write(to: url)
