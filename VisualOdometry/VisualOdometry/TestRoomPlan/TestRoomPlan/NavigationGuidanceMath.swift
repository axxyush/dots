import Foundation
import simd

struct NavigationArrowPose: Equatable {
    let position: SIMD3<Float>
    let yawRadians: Float
}

enum NavigationTurnInstruction: Equatable {
    case left
    case right
}

enum NavigationGuidanceMath {
    static func turnAngleDegrees(
        currentHeadingTransform: simd_float4x4,
        targetWorldPoint: SIMD3<Float>,
        currentWorldPoint: SIMD3<Float>
    ) -> Float {
        let heading3D = SIMD3<Float>(
            -currentHeadingTransform.columns.2.x,
            0,
            -currentHeadingTransform.columns.2.z
        )
        let targetVector3D = SIMD3<Float>(
            targetWorldPoint.x - currentWorldPoint.x,
            0,
            targetWorldPoint.z - currentWorldPoint.z
        )

        guard simd_length_squared(heading3D) > 0.0001, simd_length_squared(targetVector3D) > 0.0001 else {
            return 0
        }

        let heading = simd_normalize(heading3D)
        let target = simd_normalize(targetVector3D)
        let cross = simd_cross(heading, target).y
        let dot = simd_dot(heading, target)
        return atan2(cross, dot) * 180 / .pi
    }

    static func turnInstruction(for angleDegrees: Float, threshold: Float = 25) -> NavigationTurnInstruction? {
        guard abs(angleDegrees) > threshold else { return nil }
        return angleDegrees > 0 ? .left : .right
    }

    static func shouldAdvanceWaypoint(
        currentWorldPoint: SIMD3<Float>,
        waypoint: SIMD3<Float>,
        threshold: Float = 0.6
    ) -> Bool {
        planarDistance(currentWorldPoint, waypoint) <= threshold
    }

    static func hasArrived(
        currentWorldPoint: SIMD3<Float>,
        destinationWorldPoint: SIMD3<Float>,
        threshold: Float = 1.0
    ) -> Bool {
        planarDistance(currentWorldPoint, destinationWorldPoint) <= threshold
    }

    static func isOffPath(
        currentWorldPoint: SIMD3<Float>,
        waypoints: [SIMD3<Float>],
        threshold: Float = 1.2
    ) -> Bool {
        guard !waypoints.isEmpty else { return false }
        let nearest = waypoints.map { planarDistance(currentWorldPoint, $0) }.min() ?? 0
        return nearest > threshold
    }

    static func distanceRemaining(
        currentWorldPoint: SIMD3<Float>,
        remainingWaypoints: [SIMD3<Float>]
    ) -> Float {
        guard let first = remainingWaypoints.first else { return 0 }
        var distance = planarDistance(currentWorldPoint, first)
        for pair in zip(remainingWaypoints, remainingWaypoints.dropFirst()) {
            distance += planarDistance(pair.0, pair.1)
        }
        return distance
    }

    static func arrowPoses(
        for path: [SIMD3<Float>],
        spacing: Float = 0.8
    ) -> [NavigationArrowPose] {
        guard path.count >= 2 else { return [] }

        var results: [NavigationArrowPose] = []

        for (start, end) in zip(path, path.dropFirst()) {
            let segment = SIMD3<Float>(end.x - start.x, 0, end.z - start.z)
            let length = simd_length(segment)
            guard length > 0.001 else { continue }

            let direction = segment / length
            let yaw = atan2(direction.x, direction.z)
            let count = max(1, Int(floor(length / spacing)))

            for index in 0..<count {
                let distance = min(Float(index) * spacing + spacing / 2, length - 0.1)
                let position = start + direction * max(0.05, distance)
                results.append(NavigationArrowPose(position: position, yawRadians: yaw))
            }
        }

        return results
    }

    static func planarDistance(_ lhs: SIMD3<Float>, _ rhs: SIMD3<Float>) -> Float {
        simd_distance(SIMD2<Float>(lhs.x, lhs.z), SIMD2<Float>(rhs.x, rhs.z))
    }

    /// Calculates the absolute compass bearing (0–360) from one world point to another.
    /// Requires the AR session to be configured with `.gravityAndHeading` for meaningful results.
    static func compassBearingToWaypoint(
        from current: SIMD3<Float>,
        to target: SIMD3<Float>
    ) -> Double {
        let dx = Double(target.x - current.x)
        let dz = Double(target.z - current.z)
        // ARKit: -Z is north when using .gravityAndHeading
        var bearing = atan2(dx, -dz) * 180.0 / .pi
        if bearing < 0 { bearing += 360.0 }
        return bearing
    }

    /// Converts a compass heading (0–360) to a cardinal/intercardinal direction string.
    static func compassDirectionString(heading: Double) -> String {
        let directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        let index = Int(round(heading / 45.0)) % 8
        return directions[index < 0 ? index + 8 : index]
    }
}
