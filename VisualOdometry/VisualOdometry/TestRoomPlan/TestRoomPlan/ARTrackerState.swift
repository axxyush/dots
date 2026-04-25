import Foundation
import ARKit
import simd

// MARK: - Tracker State

/// Observable state for AR visual odometry: distance walked, step count, and compass heading.
/// Updated per-frame by `ARTrackerProcessor`.
class ARTrackerState: ObservableObject {
    @Published var distanceWalked: Float = 0.0   // In meters (signed — forward positive, backward negative)
    @Published var stepsTaken: Int = 0
    @Published var isTracking: Bool = false
    @Published var heading: Double = 0.0          // Compass heading 0–360 (requires .gravityAndHeading)

    func reset() {
        distanceWalked = 0.0
        stepsTaken = 0
    }
}

// MARK: - Tracker Processor

/// Processes ARFrames to extract distance walked, step count, and compass heading.
/// Call `processFrame(_:)` from the ARSession delegate on each frame update.
/// Does NOT own or create an ARSession — integrates with an existing one.
final class ARTrackerProcessor {
    private let state: ARTrackerState

    // Distance tracking
    private var previousPosition: SIMD3<Float>?

    // Step detection (vertical Y-axis peak detection)
    private var smoothedY: Float?
    private var isGoingUp: Bool = true
    private var lastPeakY: Float = 0.0
    private var lastTroughY: Float = 0.0

    /// Vertical bobbing threshold to register a step (~2–5 cm for normal walking)
    private let stepThreshold: Float = 0.02
    /// Hysteresis to prevent rapid direction flipping from jitter
    private let hysteresis: Float = 0.005

    init(state: ARTrackerState) {
        self.state = state
    }

    /// Call this on every `ARFrame` update from the session delegate.
    @MainActor
    func processFrame(_ frame: ARFrame) {
        let transform = frame.camera.transform

        // Update tracking state from camera tracking quality
        switch frame.camera.trackingState {
        case .normal:
            state.isTracking = true
        default:
            state.isTracking = false
        }

        let currentPos = SIMD3<Float>(
            transform.columns.3.x,
            transform.columns.3.y,
            transform.columns.3.z
        )

        // 1. Distance walked
        if let prevPos = previousPosition {
            let delta = currentPos - prevPos
            let distanceMoved = length(delta)

            // Ignore jitter below 5mm
            if distanceMoved > 0.005 {
                // Determine forward vs backward using camera heading
                let forwardVector = SIMD3<Float>(
                    -transform.columns.2.x,
                    -transform.columns.2.y,
                    -transform.columns.2.z
                )
                let dotProduct = dot(delta, forwardVector)
                let directionSign: Float = dotProduct < 0 ? -1.0 : 1.0

                state.distanceWalked += (distanceMoved * directionSign)
                previousPosition = currentPos
            }
        } else {
            previousPosition = currentPos
        }

        // 2. Step detection (vertical head bobbing)
        detectStep(currentY: currentPos.y)

        // 3. Compass heading
        // With .gravityAndHeading: yaw=0 → North, yaw=-π/2 → East, yaw=π/2 → West
        let yaw = frame.camera.eulerAngles.y
        var bearing = Double(-yaw * 180.0 / .pi)
        if bearing < 0 {
            bearing += 360.0
        }
        state.heading = bearing
    }

    /// Resets position tracking (e.g., on re-alignment). Does not reset distance/steps.
    func resetPositionTracking() {
        previousPosition = nil
        smoothedY = nil
        isGoingUp = true
        lastPeakY = 0.0
        lastTroughY = 0.0
    }

    // MARK: - Step Detection

    private func detectStep(currentY: Float) {
        guard let currentSmoothedY = smoothedY else {
            smoothedY = currentY
            lastPeakY = currentY
            lastTroughY = currentY
            return
        }

        // Low-pass filter to smooth camera jitter
        let newSmoothedY = currentSmoothedY * 0.8 + currentY * 0.2
        self.smoothedY = newSmoothedY

        if isGoingUp {
            if newSmoothedY > lastPeakY {
                lastPeakY = newSmoothedY
            } else if newSmoothedY < (lastPeakY - hysteresis) {
                // Direction changed: going down
                isGoingUp = false

                let motionAmplitude = lastPeakY - lastTroughY
                if motionAmplitude > stepThreshold {
                    Task { @MainActor in
                        state.stepsTaken += 1
                    }
                }

                lastTroughY = newSmoothedY
            }
        } else {
            if newSmoothedY < lastTroughY {
                lastTroughY = newSmoothedY
            } else if newSmoothedY > (lastTroughY + hysteresis) {
                // Direction changed: going up
                isGoingUp = true
                lastPeakY = newSmoothedY
            }
        }
    }
}

// MARK: - Compass Utilities

enum CompassUtilities {
    /// Converts a heading (0–360) to a cardinal/intercardinal direction string.
    static func directionString(for heading: Double) -> String {
        let directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        let index = Int(round(heading / 45.0)) % 8
        return directions[index < 0 ? index + 8 : index]
    }
}
