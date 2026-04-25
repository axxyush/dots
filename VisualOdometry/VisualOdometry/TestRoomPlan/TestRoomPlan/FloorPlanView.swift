import SwiftUI
import RoomPlan
import simd

struct FloorPlanView: View {
    let capturedRoom: CapturedRoom
    var selectedDoorIndex: Int? = nil

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
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: wall.transform, width: wall.dimensions.x)
            var path = Path()
            path.move(to: toScreen(a.x, a.z))
            path.addLine(to: toScreen(b.x, b.z))
            context.stroke(path, with: .color(.primary), lineWidth: 3)
        }

        // Doors
        for (i, door) in capturedRoom.doors.enumerated() {
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: door.transform, width: door.dimensions.x)
            var path = Path()
            path.move(to: toScreen(a.x, a.z))
            path.addLine(to: toScreen(b.x, b.z))
            let doorColor: Color = selectedDoorIndex == i ? .yellow : .blue
            context.stroke(path, with: .color(doorColor), lineWidth: selectedDoorIndex == i ? 7 : 5)

            let c = RoomGeometry.translation(of: door.transform)
            let pt = toScreen(c.x, c.z)
            let label = selectedDoorIndex == i ? "Entry Door" : "Door \(i)"
            context.draw(Text(label).font(.system(size: 9)).foregroundStyle(doorColor),
                         at: CGPoint(x: pt.x, y: pt.y - 12))
        }

        // Windows
        for (i, win) in capturedRoom.windows.enumerated() {
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: win.transform, width: win.dimensions.x)
            var path = Path()
            path.move(to: toScreen(a.x, a.z))
            path.addLine(to: toScreen(b.x, b.z))
            context.stroke(path, with: .color(.cyan), style: StrokeStyle(lineWidth: 3, dash: [6, 4]))

            let c = RoomGeometry.translation(of: win.transform)
            let pt = toScreen(c.x, c.z)
            context.draw(Text("Win \(i)").font(.system(size: 9)).foregroundStyle(.cyan),
                         at: CGPoint(x: pt.x, y: pt.y - 12))
        }

        // Objects
        for (i, obj) in capturedRoom.objects.enumerated() {
            let c = RoomGeometry.translation(of: obj.transform)
            let center = toScreen(c.x, c.z)
            let footprint = RoomGeometry.orientedFootprint(transform: obj.transform, dimensions: obj.dimensions)
            var path = Path()
            if let first = footprint.first {
                path.move(to: toScreen(first.x, first.y))
                for corner in footprint.dropFirst() {
                    path.addLine(to: toScreen(corner.x, corner.y))
                }
                path.closeSubpath()
            }

            context.fill(path, with: .color(.orange.opacity(0.25)))
            context.stroke(path, with: .color(.orange), lineWidth: 1.5)

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

    private func worldBounds() -> RoomSpatialBounds {
        let wallSegments = capturedRoom.walls.map {
            RoomGeometry.surfaceEndpoints(transform: $0.transform, width: $0.dimensions.x)
        }
        let doorSegments = capturedRoom.doors.map {
            RoomGeometry.surfaceEndpoints(transform: $0.transform, width: $0.dimensions.x)
        }
        let windowSegments = capturedRoom.windows.map {
            RoomGeometry.surfaceEndpoints(transform: $0.transform, width: $0.dimensions.x)
        }
        let objectFootprints = capturedRoom.objects.map {
            RoomGeometry.orientedFootprint(transform: $0.transform, dimensions: $0.dimensions)
        }

        return RoomGeometry.bounds(
            surfaces: wallSegments + doorSegments + windowSegments,
            objectFootprints: objectFootprints,
            includeOrigin: true,
            padding: 0.3
        )
    }
}
