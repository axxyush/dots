import SwiftUI
import simd

struct MiniMapView: View {
    let snapshot: CapturedRoomSnapshot
    let currentPosition: SIMD3<Float>?
    let currentCameraTransform: simd_float4x4?
    let path: [SIMD3<Float>]
    let roomWorldTransform: simd_float4x4
    
    // We compute the world-to-room transform to convert world points (like path and user position) to room space for drawing
    private var worldRoomTransform: simd_float4x4 {
        roomWorldTransform.inverse
    }
    
    var body: some View {
        GeometryReader { geo in
            Canvas { context, size in
                drawMiniMap(context: &context, size: size)
            }
        }
        .background(Color(.systemBackground).opacity(0.85))
        .clipShape(RoundedRectangle(cornerRadius: 16))
        .overlay(
            RoundedRectangle(cornerRadius: 16)
                .stroke(Color.white.opacity(0.3), lineWidth: 1)
        )
        .shadow(color: .black.opacity(0.3), radius: 10, x: 0, y: 5)
    }
    
    private func drawMiniMap(context: inout GraphicsContext, size: CGSize) {
        let bounds = snapshot.roomBounds
        let worldW = bounds.maxX - bounds.minX
        let worldD = bounds.maxZ - bounds.minZ
        guard worldW > 0, worldD > 0 else { return }
        
        let padding: CGFloat = 10
        let availW = size.width - padding * 2
        let availH = size.height - padding * 2
        let scale = min(availW / CGFloat(worldW), availH / CGFloat(worldD))
        let offX = padding + (availW - CGFloat(worldW) * scale) / 2
        let offZ = padding + (availH - CGFloat(worldD) * scale) / 2
        
        func toScreen(_ x: Float, _ z: Float) -> CGPoint {
            CGPoint(x: offX + CGFloat(x - bounds.minX) * scale,
                    y: offZ + CGFloat(z - bounds.minZ) * scale)
        }
        
        func toRoomSpace(_ worldPt: SIMD3<Float>) -> SIMD3<Float> {
            let vec = worldRoomTransform * SIMD4<Float>(worldPt.x, worldPt.y, worldPt.z, 1)
            return SIMD3<Float>(vec.x, vec.y, vec.z)
        }
        
        // Draw Walls
        for wall in snapshot.walls {
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: wall.transformMatrix.simd, width: wall.dimensionsMeters.x)
            var path = Path()
            path.move(to: toScreen(a.x, a.z))
            path.addLine(to: toScreen(b.x, b.z))
            context.stroke(path, with: .color(.primary.opacity(0.7)), lineWidth: 2)
        }
        
        // Draw Doors
        for door in snapshot.doors {
            let (a, b) = RoomGeometry.surfaceEndpoints(transform: door.transformMatrix.simd, width: door.dimensionsMeters.x)
            var path = Path()
            path.move(to: toScreen(a.x, a.z))
            path.addLine(to: toScreen(b.x, b.z))
            context.stroke(path, with: .color(.blue.opacity(0.8)), lineWidth: 3.5)
        }
        
        // Draw Objects
        for obj in snapshot.objects {
            let footprint = RoomGeometry.orientedFootprint(transform: obj.transformMatrix.simd, dimensions: obj.dimensionsMeters.simd)
            var path = Path()
            if let first = footprint.first {
                path.move(to: toScreen(first.x, first.y))
                for corner in footprint.dropFirst() {
                    path.addLine(to: toScreen(corner.x, corner.y))
                }
                path.closeSubpath()
            }
            context.fill(path, with: .color(.orange.opacity(0.2)))
            context.stroke(path, with: .color(.orange.opacity(0.8)), lineWidth: 1)
        }
        
        // Draw Path
        if !path.isEmpty {
            var routePath = Path()
            let firstRoomPt = toRoomSpace(path.first!)
            routePath.move(to: toScreen(firstRoomPt.x, firstRoomPt.z))
            for wp in path.dropFirst() {
                let roomPt = toRoomSpace(wp)
                routePath.addLine(to: toScreen(roomPt.x, roomPt.z))
            }
            context.stroke(routePath, with: .color(.green), style: StrokeStyle(lineWidth: 3, lineCap: .round, lineJoin: .round))
            
            // Draw destination pin
            if let lastWp = path.last {
                let roomPt = toRoomSpace(lastWp)
                let screenPt = toScreen(roomPt.x, roomPt.z)
                context.fill(Path(ellipseIn: CGRect(x: screenPt.x - 4, y: screenPt.y - 4, width: 8, height: 8)), with: .color(.green))
            }
        }
        
        // Draw User
        if let currentPosition = currentPosition {
            let roomPt = toRoomSpace(currentPosition)
            let screenPt = toScreen(roomPt.x, roomPt.z)
            
            // User Dot
            context.fill(Path(ellipseIn: CGRect(x: screenPt.x - 5, y: screenPt.y - 5, width: 10, height: 10)), with: .color(.blue))
            context.stroke(Path(ellipseIn: CGRect(x: screenPt.x - 5, y: screenPt.y - 5, width: 10, height: 10)), with: .color(.white), lineWidth: 1.5)
            
            // Direction Arrow
            if let cameraTransform = currentCameraTransform {
                // Get the forward vector in world space (-Z is forward for AR camera)
                _ = RoomGeometry.horizontalAxis(of: cameraTransform, column: 2) // +Z is backward, so we negate later, or use column 2 and reverse
                // Since column 2 is +Z, forward is -column 2. 
                // Wait, horizontalAxis column: 2 returns a normalized vector for Z. Forward is -Z.
                let forwardWorldVec = -RoomGeometry.horizontalAxis(of: cameraTransform, column: 2)
                
                // Convert forward vector to room space
                let forwardRoom4 = worldRoomTransform * SIMD4<Float>(forwardWorldVec.x, 0, forwardWorldVec.z, 0)
                let forwardRoom = simd_normalize(SIMD2<Float>(forwardRoom4.x, forwardRoom4.z))
                
                // Draw a small cone/arrow pointing in forwardRoom direction
                let arrowLen: CGFloat = 12
                let arrowTip = CGPoint(x: screenPt.x + CGFloat(forwardRoom.x) * arrowLen, y: screenPt.y + CGFloat(forwardRoom.y) * arrowLen)
                
                // Calculate side points for the arrow base
                let angle = atan2(CGFloat(forwardRoom.y), CGFloat(forwardRoom.x))
                let baseAngle1 = angle + .pi * 0.75
                let baseAngle2 = angle - .pi * 0.75
                let baseLen: CGFloat = 6
                
                let pt1 = CGPoint(x: screenPt.x + cos(baseAngle1) * baseLen, y: screenPt.y + sin(baseAngle1) * baseLen)
                let pt2 = CGPoint(x: screenPt.x + cos(baseAngle2) * baseLen, y: screenPt.y + sin(baseAngle2) * baseLen)
                
                var arrowPath = Path()
                arrowPath.move(to: arrowTip)
                arrowPath.addLine(to: pt1)
                arrowPath.addLine(to: pt2)
                arrowPath.closeSubpath()
                
                context.fill(arrowPath, with: .color(.blue.opacity(0.8)))
            }
        }
    }
}
