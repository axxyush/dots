import SwiftUI
import RoomPlan
import ARKit

class ScanManager: NSObject, ObservableObject, ARSessionDelegate {
    @Published var capturedPhotos: [ARCapturedPhoto] = []
    var currentSession: RoomCaptureSession?

    // Odometry tracking during scan
    let trackerState = ARTrackerState()
    private lazy var trackerProcessor = ARTrackerProcessor(state: trackerState)

    func attachToSession(_ session: RoomCaptureSession) {
        currentSession = session
        // Hook into the underlying ARSession to receive frame updates for odometry
        session.arSession.delegate = self
    }

    func takePhoto() {
        guard let session = currentSession,
              let frame = session.arSession.currentFrame else { return }

        let pixelBuffer = frame.capturedImage
        let ciImage = CIImage(cvPixelBuffer: pixelBuffer)
        let context = CIContext(options: nil)
        guard let cgImage = context.createCGImage(ciImage, from: ciImage.extent) else { return }

        // ARKit returns the image in landscape right by default.
        let uiImage = UIImage(cgImage: cgImage, scale: 1.0, orientation: .right)

        let photo = ARCapturedPhoto(
            image: uiImage,
            transform: frame.camera.transform,
            intrinsics: frame.camera.intrinsics,
            timestamp: frame.timestamp
        )

        DispatchQueue.main.async {
            self.capturedPhotos.append(photo)
        }
    }

    // MARK: - ARSessionDelegate

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        Task { @MainActor in
            self.trackerProcessor.processFrame(frame)
        }
    }
}
