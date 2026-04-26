import Foundation
import CoreVideo
import UIKit

#if canImport(ZeticMLange)
import ZeticMLange
#endif

/// On-device YOLOv8 object classifier via ZETIC Melange.
/// Runs entirely on the local NPU/CPU — no cloud calls, no privacy risk.
final class ZeticVisionClassifier: ObservableObject, @unchecked Sendable {
    @Published private(set) var isModelLoaded = false
    @Published private(set) var lastLabel: String?

    private let classifierQueue = DispatchQueue(label: "dots.zetic.vision", qos: .userInitiated)

    #if canImport(ZeticMLange)
    private var model: ZeticMLangeModel?
    #endif

    /// Loads the YOLOv8l model from Melange. Call once on app/session start.
    func loadModel() async {
        guard !isModelLoaded else { return }

        #if canImport(ZeticMLange)
        do {
            let loaded = try ZeticMLangeModel(
                personalKey: AppSecrets.zeticPersonalKey,
                name: "vaibhav-zetic/YOLOv8l",
                version: 1,
                modelMode: ModelMode.RUN_SPEED,
                onDownload: { progress in
                    print("[ZETIC Vision] Downloading model: \(Int(progress * 100))%")
                }
            )
            self.model = loaded
            self.isModelLoaded = true
            print("[ZETIC Vision] YOLOv8l model loaded successfully.")
        } catch {
            print("[ZETIC Vision] Failed to load model: \(error.localizedDescription)")
        }
        #else
        print("[ZETIC Vision] ZeticMLange framework not available. Skipping model load.")
        #endif
    }

    /// Classifies the contents of a camera frame. Returns the top-1 COCO label, or nil.
    /// Designed to be called from a background queue when an obstacle is detected.
    func classify(pixelBuffer: CVPixelBuffer) async -> String? {
        #if canImport(ZeticMLange)
        guard let model else { return nil }

        do {
            // Resize the pixel buffer to 640×640 for YOLO input
            guard let resizedData = preprocessPixelBuffer(pixelBuffer, targetSize: 640) else {
                return nil
            }

            let inputTensor = Tensor(data: resizedData, shape: [1, 3, 640, 640])
            let outputs = try model.run([inputTensor])

            // Parse YOLO output: find highest confidence detection
            guard let outputTensor = outputs.first else { return nil }
            let label = parseYOLOOutput(outputTensor)
            
            self.lastLabel = label
            return label
        } catch {
            print("[ZETIC Vision] Inference error: \(error.localizedDescription)")
            return nil
        }
        #else
        return nil
        #endif
    }

    // MARK: - Preprocessing

    /// Converts a CVPixelBuffer to a normalized Float32 tensor [1, 3, H, W] in RGB order.
    private func preprocessPixelBuffer(_ pixelBuffer: CVPixelBuffer, targetSize: Int) -> [Float]? {
        CVPixelBufferLockBaseAddress(pixelBuffer, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(pixelBuffer, .readOnly) }

        guard let baseAddress = CVPixelBufferGetBaseAddress(pixelBuffer) else { return nil }

        let width = CVPixelBufferGetWidth(pixelBuffer)
        let height = CVPixelBufferGetHeight(pixelBuffer)
        let bytesPerRow = CVPixelBufferGetBytesPerRow(pixelBuffer)

        // Create a CGImage from the pixel buffer
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        guard let context = CGContext(
            data: baseAddress,
            width: width,
            height: height,
            bitsPerComponent: 8,
            bytesPerRow: bytesPerRow,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue
        ), let cgImage = context.makeImage() else {
            return nil
        }

        // Resize to target dimensions
        guard let resizeContext = CGContext(
            data: nil,
            width: targetSize,
            height: targetSize,
            bitsPerComponent: 8,
            bytesPerRow: targetSize * 4,
            space: colorSpace,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else {
            return nil
        }

        resizeContext.draw(cgImage, in: CGRect(x: 0, y: 0, width: targetSize, height: targetSize))

        guard let resizedData = resizeContext.data else { return nil }

        let pixelCount = targetSize * targetSize
        var result = [Float](repeating: 0, count: 3 * pixelCount)
        let pointer = resizedData.bindMemory(to: UInt8.self, capacity: pixelCount * 4)

        for i in 0..<pixelCount {
            let offset = i * 4
            result[i] = Float(pointer[offset]) / 255.0                   // R
            result[pixelCount + i] = Float(pointer[offset + 1]) / 255.0  // G
            result[2 * pixelCount + i] = Float(pointer[offset + 2]) / 255.0 // B
        }

        return result
    }

    // MARK: - YOLO Output Parsing

    /// COCO 80-class labels for YOLOv8.
    private static let cocoLabels: [String] = [
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck", "boat",
        "traffic light", "fire hydrant", "stop sign", "parking meter", "bench", "bird", "cat",
        "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra", "giraffe", "backpack",
        "umbrella", "handbag", "tie", "suitcase", "frisbee", "skis", "snowboard", "sports ball",
        "kite", "baseball bat", "baseball glove", "skateboard", "surfboard", "tennis racket",
        "bottle", "wine glass", "cup", "fork", "knife", "spoon", "bowl", "banana", "apple",
        "sandwich", "orange", "broccoli", "carrot", "hot dog", "pizza", "donut", "cake",
        "chair", "couch", "potted plant", "bed", "dining table", "toilet", "tv", "laptop",
        "mouse", "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
        "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier", "toothbrush"
    ]

    #if canImport(ZeticMLange)
    /// Parses YOLO output tensor for the highest-confidence detection.
    private func parseYOLOOutput(_ tensor: Tensor) -> String? {
        let data = tensor.data
        guard data.count > 0 else { return nil }

        // YOLOv8 output shape is typically [1, 84, 8400] — 84 = 4 bbox + 80 classes
        let numClasses = 80
        let numDetections = data.count / (numClasses + 4)
        guard numDetections > 0 else { return nil }

        var bestConf: Float = 0.25 // Minimum confidence threshold
        var bestClassIdx = -1

        for det in 0..<numDetections {
            let classOffset = 4 * numDetections + det
            for cls in 0..<numClasses {
                let idx = classOffset + cls * numDetections
                guard idx < data.count else { continue }
                let conf = data[idx]
                if conf > bestConf {
                    bestConf = conf
                    bestClassIdx = cls
                }
            }
        }

        guard bestClassIdx >= 0, bestClassIdx < Self.cocoLabels.count else { return nil }
        return Self.cocoLabels[bestClassIdx]
    }
    #endif
}
