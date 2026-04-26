import SwiftUI
import RoomPlan
import simd

enum SelectedAnchor: Hashable {
    case door(Int)
    case object(Int)
}

struct FloorPlanView: View {
    let capturedRoom: CapturedRoom
    var selectedAnchor: SelectedAnchor? = nil
    var doorLabelOverrides: [Int: String] = [:]
    var objectLabelOverrides: [Int: String] = [:]
    var onSelectAnchor: ((SelectedAnchor) -> Void)? = nil

    var body: some View {
        GeometryReader { geo in
            Canvas { context, size in
                drawFloorPlan(context: &context, size: size)
            }
            .contentShape(Rectangle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onEnded { value in
                        guard let hit = hitTest(at: value.location, size: geo.size) else { return }
                        onSelectAnchor?(hit)
                    }
            )
        }
        .background(Color(.systemBackground))
    }

    // MARK: - Drawing

    private func drawFloorPlan(context: inout GraphicsContext, size: CGSize) {
        guard let layout = layout(for: size) else {
            context.draw(Text("No spatial data detected."), at: CGPoint(x: size.width / 2, y: size.height / 2))
            return
        }

        // Walls
        for wall in capturedRoom.walls {
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: wall.transform, width: wall.dimensions.x)
            var path = Path()
            path.move(to: layout.toScreen(a.x, a.z))
            path.addLine(to: layout.toScreen(b.x, b.z))
            context.stroke(path, with: .color(.primary), lineWidth: 3)
        }

        // Doors
        for (i, door) in capturedRoom.doors.enumerated() {
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: door.transform, width: door.dimensions.x)
            var path = Path()
            path.move(to: layout.toScreen(a.x, a.z))
            path.addLine(to: layout.toScreen(b.x, b.z))

            let isSelected = selectedAnchor == .door(i)
            let doorColor: Color = isSelected ? .yellow : .blue
            context.stroke(path, with: .color(doorColor), lineWidth: isSelected ? 7 : 5)

            let c = RoomGeometry.translation(of: door.transform)
            let pt = layout.toScreen(c.x, c.z)
            let label = isSelected ? "Starting Point" : doorLabel(for: i)
            context.draw(Text(label).font(.system(size: 9)).foregroundStyle(doorColor),
                         at: CGPoint(x: pt.x, y: pt.y - 12))
        }

        // Windows
        for (i, win) in capturedRoom.windows.enumerated() {
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: win.transform, width: win.dimensions.x)
            var path = Path()
            path.move(to: layout.toScreen(a.x, a.z))
            path.addLine(to: layout.toScreen(b.x, b.z))
            context.stroke(path, with: .color(.cyan), style: StrokeStyle(lineWidth: 3, dash: [6, 4]))

            let c = RoomGeometry.translation(of: win.transform)
            let pt = layout.toScreen(c.x, c.z)
            context.draw(Text("Window \(i + 1)").font(.system(size: 9)).foregroundStyle(.cyan),
                         at: CGPoint(x: pt.x, y: pt.y - 12))
        }

        // Objects
        for (i, obj) in capturedRoom.objects.enumerated() {
            let c = RoomGeometry.translation(of: obj.transform)
            let center = layout.toScreen(c.x, c.z)
            let footprint = RoomGeometry.orientedFootprint(transform: obj.transform, dimensions: obj.dimensions)
            var path = Path()
            if let first = footprint.first {
                path.move(to: layout.toScreen(first.x, first.y))
                for corner in footprint.dropFirst() {
                    path.addLine(to: layout.toScreen(corner.x, corner.y))
                }
                path.closeSubpath()
            }

            let isSelected = selectedAnchor == .object(i)
            context.fill(path, with: .color(isSelected ? .yellow.opacity(0.4) : .orange.opacity(0.25)))
            context.stroke(path, with: .color(isSelected ? .yellow : .orange), lineWidth: isSelected ? 3 : 1.5)

            let label = isSelected ? "Starting Point (\(objectLabel(for: i, object: obj)))" : objectLabel(for: i, object: obj)
            context.draw(
                Text(label).font(.system(size: 8, weight: .medium)).foregroundStyle(isSelected ? .primary : .primary),
                at: center
            )
        }

        // Origin marker (scan start point)
        let origin = layout.toScreen(0, 0)
        var crossPath = Path()
        crossPath.move(to: CGPoint(x: origin.x - 6, y: origin.y))
        crossPath.addLine(to: CGPoint(x: origin.x + 6, y: origin.y))
        crossPath.move(to: CGPoint(x: origin.x, y: origin.y - 6))
        crossPath.addLine(to: CGPoint(x: origin.x, y: origin.y + 6))
        context.stroke(crossPath, with: .color(.red), lineWidth: 2)
        context.draw(Text("Origin").font(.system(size: 8)).foregroundStyle(.red),
                     at: CGPoint(x: origin.x, y: origin.y - 12))

        // Scale bar
        drawScaleBar(context: &context, size: size, scale: layout.scale, padding: layout.padding)
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

    private func doorLabel(for index: Int) -> String {
        RoomLabeling.sanitizedOverride(doorLabelOverrides[index]) ?? RoomLabeling.defaultSurfaceLabel(category: "door", index: index)
    }

    private func objectLabel(for index: Int, object: CapturedRoom.Object) -> String {
        let category = RoomExporter.objectCategoryName(object.category)
        return RoomLabeling.sanitizedOverride(objectLabelOverrides[index]) ?? RoomLabeling.defaultObjectLabel(category: category, index: index)
    }

    private func layout(for size: CGSize) -> FloorPlanLayout? {
        let bounds = worldBounds()
        let worldW = bounds.maxX - bounds.minX
        let worldD = bounds.maxZ - bounds.minZ
        guard worldW > 0, worldD > 0 else { return nil }

        let padding: CGFloat = 50
        let scaleBarHeight: CGFloat = 40
        let availW = size.width - padding * 2
        let availH = size.height - padding * 2 - scaleBarHeight
        let scale = min(availW / CGFloat(worldW), availH / CGFloat(worldD))
        let offX = padding + (availW - CGFloat(worldW) * scale) / 2
        let offZ = padding + (availH - CGFloat(worldD) * scale) / 2

        return FloorPlanLayout(bounds: bounds, scale: scale, padding: padding, offX: offX, offZ: offZ)
    }

    private func hitTest(at point: CGPoint, size: CGSize) -> SelectedAnchor? {
        guard let layout = layout(for: size) else { return nil }

        var nearestObject: (distance: CGFloat, anchor: SelectedAnchor)?
        for (index, object) in capturedRoom.objects.enumerated() {
            let footprint = RoomGeometry.orientedFootprint(transform: object.transform, dimensions: object.dimensions)
                .map { layout.toScreen($0.x, $0.y) }

            if footprint.count >= 3 {
                let path = CGMutablePath()
                path.addLines(between: footprint)
                path.closeSubpath()
                if path.contains(point) {
                    return .object(index)
                }
            }

            let center3D = RoomGeometry.translation(of: object.transform)
            let center = layout.toScreen(center3D.x, center3D.z)
            let distance = hypot(center.x - point.x, center.y - point.y)
            if distance <= 22, (nearestObject == nil || distance < nearestObject!.distance) {
                nearestObject = (distance, .object(index))
            }
        }

        var nearestDoor: (distance: CGFloat, anchor: SelectedAnchor)?
        for (index, door) in capturedRoom.doors.enumerated() {
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: door.transform, width: door.dimensions.x)
            let start = layout.toScreen(a.x, a.z)
            let end = layout.toScreen(b.x, b.z)
            let distance = distanceFromPoint(point, toSegmentStart: start, end: end)
            if distance <= 18, (nearestDoor == nil || distance < nearestDoor!.distance) {
                nearestDoor = (distance, .door(index))
            }
        }

        if let nearestObject {
            return nearestObject.anchor
        }

        return nearestDoor?.anchor
    }

    private func distanceFromPoint(_ point: CGPoint, toSegmentStart start: CGPoint, end: CGPoint) -> CGFloat {
        let dx = end.x - start.x
        let dy = end.y - start.y
        let lengthSquared = dx * dx + dy * dy
        guard lengthSquared > 0.001 else {
            return hypot(point.x - start.x, point.y - start.y)
        }

        let projected = ((point.x - start.x) * dx + (point.y - start.y) * dy) / lengthSquared
        let t = min(1, max(0, projected))
        let closest = CGPoint(x: start.x + dx * t, y: start.y + dy * t)
        return hypot(point.x - closest.x, point.y - closest.y)
    }
}

private struct FloorPlanLayout {
    let bounds: RoomSpatialBounds
    let scale: CGFloat
    let padding: CGFloat
    let offX: CGFloat
    let offZ: CGFloat

    func toScreen(_ x: Float, _ z: Float) -> CGPoint {
        CGPoint(
            x: offX + CGFloat(x - bounds.minX) * scale,
            y: offZ + CGFloat(z - bounds.minZ) * scale
        )
    }
}
