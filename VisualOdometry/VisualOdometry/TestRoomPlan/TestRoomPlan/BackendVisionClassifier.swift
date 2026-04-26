import CoreGraphics
import CoreImage
import CoreVideo
import Foundation
import ImageIO

#if canImport(ZeticMLange)
import ZeticMLange
#endif

struct FrontObstacleWarning: Equatable {
    let label: String
    let confidence: Float
    let normalizedBounds: CGRect

    var message: String {
        "Caution: \(label) directly ahead."
    }
}

struct FrontObstacleFrameInput {
    let tensorData: Data
    let shape: [Int]
}

/// Runs YOLOv8n locally with Zetic MLange and turns the detections into
/// a simple "directly ahead" warning for navigation.
final class BackendVisionClassifier {
    private let inferenceQueue = DispatchQueue(label: "com.dots.front-obstacle-detector", qos: .userInitiated)
    private let ciContext = CIContext()
    private let inputDimension = 640
    private let minimumConfidence: Float = 0.4
    private let iouThreshold: Float = 0.45

    private var isModelLoaded = false
    private var isLoadingModel = false
    private var isProcessing = false

    #if canImport(ZeticMLange)
    private var model: ZeticMLangeModel?
    #endif

    func prepareModelIfNeeded() {
        inferenceQueue.async { [weak self] in
            guard let self else { return }
            guard !self.isModelLoaded, !self.isLoadingModel else { return }
            self.isLoadingModel = true
            defer { self.isLoadingModel = false }

            #if canImport(ZeticMLange)
            do {
                let loaded = try ZeticMLangeModel(
                    personalKey: AppSecrets.zeticPersonalKey,
                    name: "Ultralytics/YOLOv8n",
                    version: 1,
                    modelMode: .RUN_SPEED,
                    onDownload: { progress in
                        print("[ZETIC YOLO] Downloading model: \(Int(progress * 100))%")
                    }
                )
                self.model = loaded
                self.isModelLoaded = true
                print("[ZETIC YOLO] YOLOv8n model loaded successfully.")
            } catch {
                print("[ZETIC YOLO] Failed to load model: \(error.localizedDescription)")
            }
            #else
            print("[ZETIC YOLO] ZeticMLange framework not available. Skipping model load.")
            #endif
        }
    }

    func makeFrameInput(from pixelBuffer: CVPixelBuffer) -> FrontObstacleFrameInput? {
        #if canImport(ZeticMLange)
        guard isModelLoaded else { return nil }
        return makeInputTensor(from: pixelBuffer)
        #else
        return nil
        #endif
    }

    func detectFrontObstacle(
        from frameInput: FrontObstacleFrameInput,
        completion: @escaping (FrontObstacleWarning?) -> Void
    ) {
        #if canImport(ZeticMLange)
        inferenceQueue.async { [weak self] in
            guard let self else {
                DispatchQueue.main.async {
                    completion(nil)
                }
                return
            }

            guard self.isModelLoaded, let model = self.model else {
                DispatchQueue.main.async {
                    completion(nil)
                }
                return
            }

            guard !self.isProcessing else {
                DispatchQueue.main.async {
                    completion(nil)
                }
                return
            }

            self.isProcessing = true
            defer {
                self.isProcessing = false
            }

            let inputTensor = Tensor(
                data: frameInput.tensorData,
                dataType: BuiltinDataType.float32,
                shape: frameInput.shape
            )

            do {
                let outputs = try model.run(inputs: [inputTensor])
                let detections = self.parseDetections(from: outputs)
                let warning = self.selectFrontObstacle(from: detections)
                DispatchQueue.main.async {
                    completion(warning)
                }
            } catch {
                print("[ZETIC YOLO] Inference failed: \(error.localizedDescription)")
                DispatchQueue.main.async {
                    completion(nil)
                }
            }
        }
        #else
        DispatchQueue.main.async {
            completion(nil)
        }
        #endif
    }

    #if canImport(ZeticMLange)
    private func makeInputTensor(from pixelBuffer: CVPixelBuffer) -> FrontObstacleFrameInput? {
        let sourceImage = CIImage(cvPixelBuffer: pixelBuffer).oriented(.right)
        let width = inputDimension
        let height = inputDimension
        let colorSpace = CGColorSpaceCreateDeviceRGB()
        let rowBytes = width * 4

        var rgbaBytes = [UInt8](repeating: 0, count: width * height * 4)
        guard
            let bitmapContext = CGContext(
                data: &rgbaBytes,
                width: width,
                height: height,
                bitsPerComponent: 8,
                bytesPerRow: rowBytes,
                space: colorSpace,
                bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
            )
        else {
            return nil
        }

        let extent = sourceImage.extent.integral
        guard let cgImage = ciContext.createCGImage(sourceImage, from: extent) else {
            return nil
        }

        bitmapContext.interpolationQuality = .medium
        bitmapContext.draw(cgImage, in: CGRect(x: 0, y: 0, width: width, height: height))

        let planeCount = width * height
        var floats = [Float](repeating: 0, count: planeCount * 3)
        for y in 0..<height {
            for x in 0..<width {
                let pixelOffset = (y * width + x) * 4
                let planarOffset = y * width + x
                floats[planarOffset] = Float(rgbaBytes[pixelOffset]) / 255.0
                floats[planeCount + planarOffset] = Float(rgbaBytes[pixelOffset + 1]) / 255.0
                floats[(planeCount * 2) + planarOffset] = Float(rgbaBytes[pixelOffset + 2]) / 255.0
            }
        }

        let data = floats.withUnsafeBufferPointer { Data(buffer: $0) }
        return FrontObstacleFrameInput(tensorData: data, shape: [1, 3, height, width])
    }

    private func parseDetections(from outputs: [Tensor]) -> [Detection] {
        guard let primary = outputs.first else { return [] }
        let floatCount = primary.data.count / MemoryLayout<Float>.size
        var values = [Float](repeating: 0, count: floatCount)
        _ = values.withUnsafeMutableBytes { primary.data.copyBytes(to: $0) }

        let shape = primary.shape
        let format = TensorFormat(shape: shape)
        guard format.boxCount > 0, format.channelCount >= 6 else {
            print("[ZETIC YOLO] Unsupported output shape: \(shape)")
            return []
        }

        var detections: [Detection] = []
        detections.reserveCapacity(format.boxCount / 4)

        for boxIndex in 0..<format.boxCount {
            let x: Float
            let y: Float
            let w: Float
            let h: Float
            let classSlice: ArraySlice<Float>
            let objectness: Float

            switch format.layout {
            case .channelsFirst:
                x = values[boxIndex]
                y = values[format.boxCount + boxIndex]
                w = values[(format.boxCount * 2) + boxIndex]
                h = values[(format.boxCount * 3) + boxIndex]

                let classStart = format.classOffset * format.boxCount + boxIndex
                classSlice = stride(from: classStart, to: classStart + format.classCount * format.boxCount, by: format.boxCount)
                    .map { values[$0] }[...]
                objectness = format.hasObjectness ? values[(4 * format.boxCount) + boxIndex] : 1

            case .boxesFirst:
                let base = boxIndex * format.channelCount
                x = values[base]
                y = values[base + 1]
                w = values[base + 2]
                h = values[base + 3]
                objectness = format.hasObjectness ? values[base + 4] : 1
                let start = base + format.classOffset
                classSlice = values[start..<(start + format.classCount)]
            }

            guard let (classIndex, classScore) = classSlice.enumerated().max(by: { $0.element < $1.element }) else {
                continue
            }

            let confidence = classScore * objectness
            guard confidence >= minimumConfidence else { continue }

            let rect = normalizedRect(centerX: x, centerY: y, width: w, height: h)
            guard rect.width > 0.02, rect.height > 0.02 else { continue }
            guard rect.maxX > 0, rect.minX < 1, rect.maxY > 0, rect.minY < 1 else { continue }

            detections.append(
                Detection(
                    label: Self.cocoLabels[safe: classIndex] ?? "object",
                    confidence: confidence,
                    rect: rect.standardized
                )
            )
        }

        return nonMaxSuppression(detections)
    }

    private func normalizedRect(centerX: Float, centerY: Float, width: Float, height: Float) -> CGRect {
        let scale = Float(inputDimension)
        let normalizedWidth = CGFloat(width / scale)
        let normalizedHeight = CGFloat(height / scale)
        let originX = CGFloat((centerX / scale) - (width / scale / 2))
        let originY = CGFloat((centerY / scale) - (height / scale / 2))
        return CGRect(x: originX, y: originY, width: normalizedWidth, height: normalizedHeight)
            .intersection(CGRect(x: 0, y: 0, width: 1, height: 1))
    }

    private func nonMaxSuppression(_ detections: [Detection]) -> [Detection] {
        let sorted = detections.sorted { $0.confidence > $1.confidence }
        var kept: [Detection] = []

        for detection in sorted {
            if kept.contains(where: { iou(between: $0.rect, and: detection.rect) > iouThreshold }) {
                continue
            }
            kept.append(detection)
        }

        return kept
    }

    private func iou(between lhs: CGRect, and rhs: CGRect) -> Float {
        let intersection = lhs.intersection(rhs)
        guard !intersection.isNull else { return 0 }
        let intersectionArea = intersection.width * intersection.height
        let unionArea = lhs.width * lhs.height + rhs.width * rhs.height - intersectionArea
        guard unionArea > 0 else { return 0 }
        return Float(intersectionArea / unionArea)
    }

    private func selectFrontObstacle(from detections: [Detection]) -> FrontObstacleWarning? {
        let corridor = CGRect(x: 0.3, y: 0.25, width: 0.4, height: 0.75)

        let candidate = detections
            .filter { Self.collisionRelevantLabels.contains($0.label) }
            .filter { $0.rect.intersects(corridor) }
            .filter { $0.rect.maxY >= 0.55 }
            .map { detection -> (Detection, Float) in
                let centerOffset = abs(Float(detection.rect.midX) - 0.5)
                let alignmentScore = max(0, 1 - (centerOffset / 0.2))
                let coverageScore = Float(detection.rect.width * detection.rect.height) * 3.5
                let verticalScore = Float(detection.rect.maxY)
                let score = detection.confidence * 0.35 + alignmentScore * 0.25 + coverageScore * 0.2 + verticalScore * 0.2
                return (detection, score)
            }
            .filter { $0.1 >= 0.58 }
            .max { $0.1 < $1.1 }?.0

        guard let candidate else { return nil }
        return FrontObstacleWarning(
            label: candidate.label,
            confidence: candidate.confidence,
            normalizedBounds: candidate.rect
        )
    }
    #endif
}

#if canImport(ZeticMLange)
private struct Detection: Equatable {
    let label: String
    let confidence: Float
    let rect: CGRect
}

private struct TensorFormat {
    enum Layout {
        case channelsFirst
        case boxesFirst
    }

    let layout: Layout
    let channelCount: Int
    let boxCount: Int
    let hasObjectness: Bool
    let classOffset: Int
    let classCount: Int

    init(shape: [Int]) {
        let dims = shape.filter { $0 > 1 }
        if dims.count >= 2 {
            let second = dims[1]
            let first = dims[0]
            if second >= first {
                layout = .channelsFirst
                channelCount = first
                boxCount = second
            } else {
                layout = .boxesFirst
                channelCount = second
                boxCount = first
            }
        } else if shape.count == 1 {
            layout = .boxesFirst
            channelCount = shape[0]
            boxCount = 1
        } else {
            layout = .boxesFirst
            channelCount = 0
            boxCount = 0
        }

        hasObjectness = channelCount > 84
        classOffset = hasObjectness ? 5 : 4
        classCount = max(0, channelCount - classOffset)
    }
}

private extension BackendVisionClassifier {
    static let collisionRelevantLabels: Set<String> = [
        "person", "bicycle", "motorcycle", "bench", "chair", "couch", "potted plant",
        "bed", "dining table", "toilet", "tv", "suitcase", "backpack", "handbag",
        "dog", "cat", "refrigerator", "oven", "microwave"
    ]

    static let cocoLabels: [String] = [
        "person", "bicycle", "car", "motorcycle", "airplane", "bus", "train", "truck",
        "boat", "traffic light", "fire hydrant", "stop sign", "parking meter", "bench",
        "bird", "cat", "dog", "horse", "sheep", "cow", "elephant", "bear", "zebra",
        "giraffe", "backpack", "umbrella", "handbag", "tie", "suitcase", "frisbee",
        "skis", "snowboard", "sports ball", "kite", "baseball bat", "baseball glove",
        "skateboard", "surfboard", "tennis racket", "bottle", "wine glass", "cup",
        "fork", "knife", "spoon", "bowl", "banana", "apple", "sandwich", "orange",
        "broccoli", "carrot", "hot dog", "pizza", "donut", "cake", "chair", "couch",
        "potted plant", "bed", "dining table", "toilet", "tv", "laptop", "mouse",
        "remote", "keyboard", "cell phone", "microwave", "oven", "toaster", "sink",
        "refrigerator", "book", "clock", "vase", "scissors", "teddy bear", "hair drier",
        "toothbrush"
    ]
}

private extension Array {
    subscript(safe index: Int) -> Element? {
        guard indices.contains(index) else { return nil }
        return self[index]
    }
}
#endif
