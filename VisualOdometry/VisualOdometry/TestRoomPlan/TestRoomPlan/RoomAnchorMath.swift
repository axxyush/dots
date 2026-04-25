import Foundation
import ARKit
import simd

enum RoomAnchorMath {
    static func roomWorldTransform(
        qrAnchorWorld: simd_float4x4,
        entryAnchorRoom: simd_float4x4
    ) -> simd_float4x4 {
        qrAnchorWorld * entryAnchorRoom.inverse
    }
}

enum TrackingQuality: Equatable {
    case initializing
    case limited(reason: String)
    case normal
}

enum TrackingGateDecision: Equatable {
    case ready
    case holdStill(message: String)
}

enum TrackingGate {
    static func decision(for quality: TrackingQuality) -> TrackingGateDecision {
        switch quality {
        case .normal:
            return .ready
        case .initializing:
            return .holdStill(message: "Move slowly while ARKit establishes tracking.")
        case .limited:
            return .holdStill(message: "Hold still until tracking improves.")
        }
    }
}

extension TrackingQuality {
    init(trackingState: ARCamera.TrackingState) {
        switch trackingState {
        case .normal:
            self = .normal
        case .limited(let reason):
            switch reason {
            case .excessiveMotion:
                self = .limited(reason: "Too much motion.")
            case .insufficientFeatures:
                self = .limited(reason: "More visual detail is needed.")
            case .initializing:
                self = .initializing
            case .relocalizing:
                self = .limited(reason: "ARKit is relocalizing.")
            @unknown default:
                self = .limited(reason: "Tracking is limited.")
            }
        case .notAvailable:
            self = .limited(reason: "Tracking is unavailable.")
        }
    }
}
