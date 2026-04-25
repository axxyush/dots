import SwiftUI
import RoomPlan
import simd

struct FloorPlanView: View {
    let capturedRoom: CapturedRoom

    var body: some View {
        GeometryReader { geo in
            Canvas { context, size in
                drawFloorPlan(context: &context, size: size)
            }
        }
        .background(Color(.systemBackground))
    }

    // MARK: - Drawing

    private func drawFloorPlan(context: inout GraphicsContext, size: CGSize) {
        let bounds = worldBounds()
        let worldW = bounds.maxX - bounds.minX
        let worldD = bounds.maxZ - bounds.minZ
        guard worldW > 0, worldD > 0 else {
            context.draw(Text("No spatial data detected."), at: CGPoint(x: size.width / 2, y: size.height / 2))
            return
        }

        let padding: CGFloat = 50
        let scaleBarHeight: CGFloat = 40
        let availW = size.width - padding * 2
        let availH = size.height - padding * 2 - scaleBarHeight
        let scale = min(availW / CGFloat(worldW), availH / CGFloat(worldD))
        let offX = padding + (availW - CGFloat(worldW) * scale) / 2
        let offZ = padding + (availH - CGFloat(worldD) * scale) / 2

        func toScreen(_ x: Float, _ z: Float) -> CGPoint {
            CGPoint(x: offX + CGFloat(x - bounds.minX) * scale,
                    y: offZ + CGFloat(z - bounds.minZ) * scale)
        }

        // Walls
        for wall in capturedRoom.walls {
            let (a, b) = wallEndpoints(wall)
            var path = Path()
            path.move(to: toScreen(a.x, a.z))
            path.addLine(to: toScreen(b.x, b.z))
            context.stroke(path, with: .color(.primary), lineWidth: 3)
        }

        // Doors
        for (i, door) in capturedRoom.doors.enumerated() {
            let (a, b) = wallEndpoints(door)
            var path = Path()
            path.move(to: toScreen(a.x, a.z))
            path.addLine(to: toScreen(b.x, b.z))
            context.stroke(path, with: .color(.blue), lineWidth: 5)

            let c = door.transform.columns.3
            let pt = toScreen(c.x, c.z)
            context.draw(Text("Door \(i)").font(.system(size: 9)).foregroundStyle(.blue),
                         at: CGPoint(x: pt.x, y: pt.y - 12))
        }

        // Windows
        for (i, win) in capturedRoom.windows.enumerated() {
            let (a, b) = wallEndpoints(win)
            var path = Path()
            path.move(to: toScreen(a.x, a.z))
            path.addLine(to: toScreen(b.x, b.z))
            context.stroke(path, with: .color(.cyan), style: StrokeStyle(lineWidth: 3, dash: [6, 4]))

            let c = win.transform.columns.3
            let pt = toScreen(c.x, c.z)
            context.draw(Text("Win \(i)").font(.system(size: 9)).foregroundStyle(.cyan),
                         at: CGPoint(x: pt.x, y: pt.y - 12))
        }

        // Objects
        for (i, obj) in capturedRoom.objects.enumerated() {
            let c = obj.transform.columns.3
            let center = toScreen(c.x, c.z)
            let w = max(CGFloat(obj.dimensions.x) * scale, 14)
            let d = max(CGFloat(obj.dimensions.z) * scale, 14)
            let rect = CGRect(x: center.x - w / 2, y: center.y - d / 2, width: w, height: d)

            context.fill(Path(rect), with: .color(.orange.opacity(0.25)))
            context.stroke(Path(rect), with: .color(.orange), lineWidth: 1.5)

            let name = RoomExporter.objectCategoryName(obj.category)
            context.draw(
                Text("\(name) #\(i)").font(.system(size: 8, weight: .medium)),
                at: center
            )
        }

        // Origin marker (scan start point)
        let origin = toScreen(0, 0)
        var crossPath = Path()
        crossPath.move(to: CGPoint(x: origin.x - 6, y: origin.y))
        crossPath.addLine(to: CGPoint(x: origin.x + 6, y: origin.y))
        crossPath.move(to: CGPoint(x: origin.x, y: origin.y - 6))
        crossPath.addLine(to: CGPoint(x: origin.x, y: origin.y + 6))
        context.stroke(crossPath, with: .color(.red), lineWidth: 2)
        context.draw(Text("Origin").font(.system(size: 8)).foregroundStyle(.red),
                     at: CGPoint(x: origin.x, y: origin.y - 12))

        // Scale bar
        drawScaleBar(context: &context, size: size, scale: scale, padding: padding)
    }

    // MARK: - Scale Bar

    private func drawScaleBar(context: inout GraphicsContext, size: CGSize, scale: CGFloat, padding: CGFloat) {
        let barY = size.height - 20
        let barLen = 1.0 * scale  // 1 meter
        let startX = padding

        var path = Path()
        path.move(to: CGPoint(x: startX, y: barY))
        path.addLine(to: CGPoint(x: startX + barLen, y: barY))
        // end ticks
        path.move(to: CGPoint(x: startX, y: barY - 5))
        path.addLine(to: CGPoint(x: startX, y: barY + 5))
        path.move(to: CGPoint(x: startX + barLen, y: barY - 5))
        path.addLine(to: CGPoint(x: startX + barLen, y: barY + 5))

        context.stroke(path, with: .color(.secondary), lineWidth: 2)
        context.draw(Text("1 m").font(.caption2),
                     at: CGPoint(x: startX + barLen / 2, y: barY - 12))
    }

    // MARK: - Helpers

    private func wallEndpoints(_ surface: CapturedRoom.Surface) -> (simd_float3, simd_float3) {
        let c = surface.transform.columns.3
        let hw = surface.dimensions.x / 2
        let dir = simd_normalize(simd_float3(surface.transform.columns.0.x,
                                              surface.transform.columns.0.y,
                                              surface.transform.columns.0.z))
        let center = simd_float3(c.x, c.y, c.z)
        return (center + dir * hw, center - dir * hw)
    }

    private struct WorldBounds {
        var minX: Float = .infinity, maxX: Float = -.infinity
        var minZ: Float = .infinity, maxZ: Float = -.infinity

        mutating func include(x: Float, z: Float) {
            minX = min(minX, x); maxX = max(maxX, x)
            minZ = min(minZ, z); maxZ = max(maxZ, z)
        }
    }

    private func worldBounds() -> WorldBounds {
        var b = WorldBounds()

        for wall in capturedRoom.walls {
            let (a, e) = wallEndpoints(wall)
            b.include(x: a.x, z: a.z)
            b.include(x: e.x, z: e.z)
        }
        for door in capturedRoom.doors {
            let (a, e) = wallEndpoints(door)
            b.include(x: a.x, z: a.z)
            b.include(x: e.x, z: e.z)
        }
        for win in capturedRoom.windows {
            let (a, e) = wallEndpoints(win)
            b.include(x: a.x, z: a.z)
            b.include(x: e.x, z: e.z)
        }
        for obj in capturedRoom.objects {
            let p = obj.transform.columns.3
            let hw = obj.dimensions.x / 2
            let hd = obj.dimensions.z / 2
            b.include(x: p.x - hw, z: p.z - hd)
            b.include(x: p.x + hw, z: p.z + hd)
        }

        // include origin
        b.include(x: 0, z: 0)

        // add margin
        let pad: Float = 0.3
        b.minX -= pad; b.maxX += pad
        b.minZ -= pad; b.maxZ += pad

        return b
    }
}
