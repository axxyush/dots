import SwiftUI
import ARKit
import SceneKit
import Combine

// MARK: - App State
class ARTrackerState: ObservableObject {
    @Published var distanceWalked: Float = 0.0 // In meters
    @Published var stepsTaken: Int = 0
    @Published var isTracking: Bool = false
    @Published var heading: Double = 0.0 // Compass heading (0-360)
    
    func reset() {
        distanceWalked = 0.0
        stepsTaken = 0
    }
}

// MARK: - Compass UI
struct CompassView: View {
    let heading: Double
    
    var directionString: String {
        let directions = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
        let index = Int(round(heading / 45.0)) % 8
        return directions[index < 0 ? index + 8 : index]
    }
    
    var body: some View {
        VStack(spacing: 4) {
            ZStack {
                Circle()
                    .stroke(lineWidth: 3)
                    .foregroundColor(.secondary.opacity(0.3))
                    .frame(width: 40, height: 40)
                
                Image(systemName: "location.north.fill")
                    .resizable()
                    .scaledToFit()
                    .frame(width: 14, height: 14)
                    .foregroundColor(.red)
                    // Rotate the arrow to point towards North
                    .rotationEffect(.degrees(-heading))
            }
            
            Text("\(Int(heading))° \(directionString)")
                .font(.system(size: 10, weight: .bold))
                .foregroundColor(.secondary)
        }
    }
}

// MARK: - Main SwiftUI View
public struct ARTrackerView: View {
    @StateObject private var trackerState = ARTrackerState()
    
    public init() {}
    
    public var body: some View {
        ZStack {
            // AR Camera Feed
            ARViewContainer(trackerState: trackerState)
                .edgesIgnoringSafeArea(.all)
            
            // UI Overlay
            VStack {
                // Stats Card
                VStack(spacing: 12) {
                    HStack {
                        Text("Visual Odometry Tracker")
                            .font(.headline)
                            .foregroundColor(.secondary)
                            
                        Spacer()
                        
                        CompassView(heading: trackerState.heading)
                    }
                    
                    HStack(spacing: 40) {
                        VStack {
                            Text(String(format: "%.2f m", trackerState.distanceWalked))
                                .font(.system(size: 32, weight: .bold))
                            Text("Distance")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                        
                        VStack {
                            Text("\(trackerState.stepsTaken)")
                                .font(.system(size: 32, weight: .bold))
                            Text("Steps")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }
                }
                .padding()
                .background(.regularMaterial)
                .cornerRadius(16)
                .shadow(radius: 10)
                .padding(.top, 40)
                
                Spacer()
                
                // Controls
                HStack {
                    if trackerState.isTracking {
                        Text("Tracking Active")
                            .font(.subheadline)
                            .foregroundColor(.green)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 8)
                            .background(Color.green.opacity(0.2))
                            .cornerRadius(8)
                    } else {
                        Text("Initializing...")
                            .font(.subheadline)
                            .foregroundColor(.orange)
                            .padding(.horizontal, 16)
                            .padding(.vertical, 8)
                            .background(Color.orange.opacity(0.2))
                            .cornerRadius(8)
                    }
                    
                    Spacer()
                    
                    Button(action: {
                        trackerState.reset()
                    }) {
                        Text("Reset")
                            .fontWeight(.semibold)
                            .foregroundColor(.white)
                            .padding(.horizontal, 24)
                            .padding(.vertical, 12)
                            .background(Color.blue)
                            .cornerRadius(12)
                            .shadow(radius: 5)
                    }
                }
                .padding(.horizontal, 20)
                .padding(.bottom, 40)
            }
        }
    }
}

// MARK: - ARKit Representable
struct ARViewContainer: UIViewRepresentable {
    @ObservedObject var trackerState: ARTrackerState
    
    func makeUIView(context: Context) -> ARSCNView {
        let arView = ARSCNView(frame: .zero)
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravityAndHeading // Aligns Z-axis with true North
        
        // These can be useful if your device supports them (LiDAR)
        if ARWorldTrackingConfiguration.supportsSceneReconstruction(.meshWithClassification) {
            config.sceneReconstruction = .meshWithClassification
        }
        if ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth) {
            config.frameSemantics.insert(.sceneDepth)
        }
        
        arView.session.delegate = context.coordinator
        arView.session.run(config, options: [.resetTracking, .removeExistingAnchors])
        
        return arView
    }
    
    func updateUIView(_ uiView: ARSCNView, context: Context) {}
    
    func makeCoordinator() -> ARTrackerCoordinator {
        ARTrackerCoordinator(trackerState: trackerState)
    }
}

// MARK: - ARSession Delegate (Logic Core)
class ARTrackerCoordinator: NSObject, ARSessionDelegate {
    var trackerState: ARTrackerState
    
    // Distance Tracking
    private var previousPosition: SIMD3<Float>?
    
    // Step Detection (Y-axis Peak Detection)
    private var smoothedY: Float?
    private var isGoingUp: Bool = true
    private var lastPeakY: Float = 0.0
    private var lastTroughY: Float = 0.0
    
    // The amount of vertical bobbing (meters) required to register a step
    // Standard walking creates roughly 2-5cm of vertical head motion
    private let stepThreshold: Float = 0.02
    // Hysteresis prevents tiny jitters from rapidly flipping the direction
    private let hysteresis: Float = 0.005
    
    init(trackerState: ARTrackerState) {
        self.trackerState = trackerState
    }
    
    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        // Run updates on main thread to sync with SwiftUI state
        DispatchQueue.main.async {
            self.trackerState.isTracking = true
            self.processFrame(frame)
        }
    }
    
    func session(_ session: ARSession, cameraDidChangeTrackingState camera: ARCamera) {
        DispatchQueue.main.async {
            switch camera.trackingState {
            case .normal:
                self.trackerState.isTracking = true
            default:
                self.trackerState.isTracking = false
            }
        }
    }
    
    private func processFrame(_ frame: ARFrame) {
        let transform = frame.camera.transform
        
        // Current position in world space
        let currentPos = SIMD3<Float>(
            transform.columns.3.x,
            transform.columns.3.y,
            transform.columns.3.z
        )
        
        // 1. Calculate Distance Walked
        if let prevPos = previousPosition {
            let delta = currentPos - prevPos
            
            // Ignore tiny jitters to prevent phantom distance accumulation
            let distanceMoved = length(delta)
            if distanceMoved > 0.005 { // 5mm threshold
                // Calculate which way the camera is facing
                // The camera looks down the negative Z-axis of its local coordinate space
                let forwardVector = SIMD3<Float>(
                    -transform.columns.2.x,
                    -transform.columns.2.y,
                    -transform.columns.2.z
                )
                
                // Dot product tells us if the movement delta is in the same direction as the camera is facing
                // Positive dot product = moving forward or sideways/up (angle < 90 deg)
                // Negative dot product = moving backward (angle > 90 deg)
                let dotProduct = dot(delta, forwardVector)
                let directionSign: Float = dotProduct < 0 ? -1.0 : 1.0
                
                trackerState.distanceWalked += (distanceMoved * directionSign)
                previousPosition = currentPos
            }
        } else {
            // First frame initialization
            previousPosition = currentPos
        }
        
        // 2. Step Detection (using vertical head bobbing)
        let currentY = currentPos.y
        detectStep(currentY: currentY)
        
        // 3. Update Compass Heading
        // eulerAngles.y (yaw) is rotation around Y-axis.
        // With .gravityAndHeading, -Z is North. Yaw=0 is North, Yaw=-pi/2 is East, Yaw=pi/2 is West.
        let yaw = frame.camera.eulerAngles.y
        var bearing = Double(-yaw * 180.0 / .pi)
        if bearing < 0 {
            bearing += 360.0
        }
        trackerState.heading = bearing
    }
    
    private func detectStep(currentY: Float) {
        guard let currentSmoothedY = smoothedY else {
            smoothedY = currentY
            lastPeakY = currentY
            lastTroughY = currentY
            return
        }
        
        // Simple Low-Pass Filter to smooth out camera jitter
        let newSmoothedY = currentSmoothedY * 0.8 + currentY * 0.2
        self.smoothedY = newSmoothedY
        
        if isGoingUp {
            if newSmoothedY > lastPeakY {
                // Keep pushing the peak higher
                lastPeakY = newSmoothedY
            } else if newSmoothedY < (lastPeakY - hysteresis) {
                // We changed direction and went down enough to surpass hysteresis
                isGoingUp = false
                
                // If the difference between the last trough and this peak is significant, it's a step!
                let motionAmplitude = lastPeakY - lastTroughY
                if motionAmplitude > stepThreshold {
                    trackerState.stepsTaken += 1
                }
                
                // Start tracking the new trough
                lastTroughY = newSmoothedY
            }
        } else {
            if newSmoothedY < lastTroughY {
                // Keep pushing the trough lower
                lastTroughY = newSmoothedY
            } else if newSmoothedY > (lastTroughY + hysteresis) {
                // We changed direction and went up enough to surpass hysteresis
                isGoingUp = true
                
                // Start tracking the new peak
                lastPeakY = newSmoothedY
            }
        }
    }
}
