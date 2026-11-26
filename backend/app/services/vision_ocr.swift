import Foundation
import CoreGraphics
import ImageIO
import Vision

struct OcrLineOut: Encodable {
    let text: String
    let x: Double
    let y: Double
    let w: Double
    let h: Double
    let confidence: Double
}

struct ImageResultOut: Encodable {
    let ok: Bool
    let width: Int
    let height: Int
    let lines: [OcrLineOut]
    let error: String?
}

struct PageResultOut: Encodable {
    let page: Int
    let ok: Bool
    let width: Int
    let height: Int
    let lines: [OcrLineOut]
    let error: String?
}

struct ServerCommand: Decodable {
    let cmd: String
    let path: String?
    let accurate: Bool?
}

let outputLock = NSLock()
let jsonEncoder = JSONEncoder()

func writeJsonLine<T: Encodable>(_ value: T) {
    guard let data = try? jsonEncoder.encode(value),
          let str = String(data: data, encoding: .utf8) else {
        return
    }
    outputLock.lock()
    defer { outputLock.unlock() }
    if let lineData = (str + "\n").data(using: .utf8) {
        FileHandle.standardOutput.write(lineData)
        fflush(stdout)
    }
}

func recognizeCGImage(_ cgImage: CGImage, accurate: Bool) throws -> [OcrLineOut] {
    let request = VNRecognizeTextRequest()
    request.recognitionLevel = accurate ? .accurate : .fast
    request.usesLanguageCorrection = true
    request.recognitionLanguages = ["zh-Hans", "zh-Hant", "en-US"]
    if #available(macOS 13.0, *) {
        request.automaticallyDetectsLanguage = true
    }

    let handler = VNImageRequestHandler(cgImage: cgImage, options: [:])
    try handler.perform([request])

    guard let observations = request.results else {
        return []
    }

    var out: [OcrLineOut] = []
    out.reserveCapacity(observations.count)
    for obs in observations {
        guard let cand = obs.topCandidates(1).first else { continue }
        let text = cand.string.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.isEmpty { continue }

        let box = obs.boundingBox
        let x = max(0.0, min(1.0, Double(box.origin.x)))
        let w = max(0.0, min(1.0 - x, Double(box.size.width)))
        let rawY = 1.0 - Double(box.origin.y) - Double(box.size.height)
        let y = max(0.0, min(1.0, rawY))
        let h = max(0.0, min(1.0 - y, Double(box.size.height)))

        out.append(OcrLineOut(
            text: text,
            x: x,
            y: y,
            w: w,
            h: h,
            confidence: Double(cand.confidence)
        ))
    }
    return out
}

func handleOcrImage(path: String, accurate: Bool) {
    let url = URL(fileURLWithPath: path)
    guard let src = CGImageSourceCreateWithURL(url as CFURL, nil),
          let cgImg = CGImageSourceCreateImageAtIndex(src, 0, nil) else {
        writeJsonLine(ImageResultOut(ok: false, width: 0, height: 0, lines: [], error: "Failed to load image: \(path)"))
        return
    }
    do {
        let lines = try recognizeCGImage(cgImg, accurate: accurate)
        writeJsonLine(ImageResultOut(ok: true, width: cgImg.width, height: cgImg.height, lines: lines, error: nil))
    } catch {
        writeJsonLine(ImageResultOut(ok: false, width: cgImg.width, height: cgImg.height, lines: [], error: error.localizedDescription))
    }
}

func runServer() {
    while let rawLine = readLine(strippingNewline: true) {
        let trimmed = rawLine.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty { continue }
        guard let data = trimmed.data(using: .utf8),
              let cmd = try? JSONDecoder().decode(ServerCommand.self, from: data) else {
            writeJsonLine(ImageResultOut(ok: false, width: 0, height: 0, lines: [], error: "Invalid JSON command"))
            continue
        }
        if cmd.cmd == "ping" {
            writeJsonLine(ImageResultOut(ok: true, width: 0, height: 0, lines: [], error: nil))
        } else if cmd.cmd == "ocr_image", let path = cmd.path {
            autoreleasepool {
                handleOcrImage(path: path, accurate: cmd.accurate ?? true)
            }
        } else if cmd.cmd == "quit" {
            break
        } else {
            writeJsonLine(ImageResultOut(ok: false, width: 0, height: 0, lines: [], error: "Unknown command: \(cmd.cmd)"))
        }
    }
}

func parsePageNumbers(_ spec: String) -> [Int] {
    var pages: [Int] = []
    for part in spec.split(separator: ",") {
        let s = part.trimmingCharacters(in: .whitespaces)
        if s.isEmpty { continue }
        if s.contains("-") {
            let bounds = s.split(separator: "-", maxSplits: 1)
            if bounds.count == 2, let a = Int(bounds[0]), let b = Int(bounds[1]), a <= b {
                for p in a...b { pages.append(p) }
            }
        } else if let p = Int(s) {
            pages.append(p)
        }
    }
    return pages
}

func renderPDFPage(_ page: CGPDFPage, maxDim: CGFloat) -> (CGImage, Int, Int)? {
    let cropBox = page.getBoxRect(.cropBox)
    let rot = abs(Int(page.rotationAngle)) % 360
    let baseW = (rot == 90 || rot == 270) ? cropBox.height : cropBox.width
    let baseH = (rot == 90 || rot == 270) ? cropBox.width : cropBox.height
    if baseW <= 1 || baseH <= 1 { return nil }

    let scale = min(2.0, max(1.0, maxDim / max(baseW, baseH, 1.0)))
    let targetW = max(16, Int((baseW * scale).rounded()))
    let targetH = max(16, Int((baseH * scale).rounded()))

    let colorSpace = CGColorSpaceCreateDeviceRGB()
    guard let ctx = CGContext(
        data: nil,
        width: targetW,
        height: targetH,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ) else {
        return nil
    }

    ctx.setFillColor(CGColor.white)
    ctx.fill(CGRect(x: 0, y: 0, width: targetW, height: targetH))
    ctx.interpolationQuality = .high
    let transform = page.getDrawingTransform(.cropBox, rect: CGRect(x: 0, y: 0, width: targetW, height: targetH), rotate: 0, preserveAspectRatio: true)
    ctx.concatenate(transform)
    ctx.drawPDFPage(page)

    guard let cgImg = ctx.makeImage() else { return nil }
    return (cgImg, targetW, targetH)
}

func runPdfBatch(pdfPath: String, pagesSpec: String, workers: Int, maxDim: CGFloat, accurate: Bool) {
    let pdfUrl = URL(fileURLWithPath: pdfPath) as CFURL
    let pages = parsePageNumbers(pagesSpec)
    if pages.isEmpty {
        return
    }

    let workerCount = max(1, min(8, workers, pages.count))
    var docPool: [CGPDFDocument] = []
    docPool.reserveCapacity(workerCount)
    for _ in 0..<workerCount {
        guard let doc = CGPDFDocument(pdfUrl) else {
            fputs("Failed to open PDF: \(pdfPath)\n", stderr)
            exit(2)
        }
        docPool.append(doc)
    }

    var slotQueue = Array(0..<workerCount)
    let slotLock = NSLock()
    let semaphore = DispatchSemaphore(value: workerCount)
    let queue = DispatchQueue(label: "inkmind.vision.pdfbatch", attributes: .concurrent)
    let group = DispatchGroup()

    for pageNum in pages {
        semaphore.wait()
        group.enter()
        queue.async {
            var slotIdx = 0
            slotLock.lock()
            slotIdx = slotQueue.removeLast()
            slotLock.unlock()

            defer {
                slotLock.lock()
                slotQueue.append(slotIdx)
                slotLock.unlock()
                semaphore.signal()
                group.leave()
            }

            autoreleasepool {
                let doc = docPool[slotIdx]
                guard let page = doc.page(at: pageNum) else {
                    writeJsonLine(PageResultOut(page: pageNum, ok: false, width: 0, height: 0, lines: [], error: "Invalid page number \(pageNum)"))
                    return
                }
                guard let (cgImg, w, h) = renderPDFPage(page, maxDim: maxDim) else {
                    writeJsonLine(PageResultOut(page: pageNum, ok: false, width: 0, height: 0, lines: [], error: "Failed to render page \(pageNum)"))
                    return
                }
                do {
                    let lines = try recognizeCGImage(cgImg, accurate: accurate)
                    writeJsonLine(PageResultOut(page: pageNum, ok: true, width: w, height: h, lines: lines, error: nil))
                } catch {
                    writeJsonLine(PageResultOut(page: pageNum, ok: false, width: w, height: h, lines: [], error: error.localizedDescription))
                }
            }
        }
    }

    group.wait()
}

let args = CommandLine.arguments
if args.count >= 2 && args[1] == "--server" {
    runServer()
} else if args.count >= 6 && args[1] == "--pdf-batch" {
    let pdfPath = args[2]
    let pagesSpec = args[3]
    let workers = Int(args[4]) ?? 4
    let maxDim = CGFloat(Double(args[5]) ?? 1500.0)
    let accurate = args.count >= 7 ? (args[6] != "0" && args[6].lowercased() != "false") : true
    runPdfBatch(pdfPath: pdfPath, pagesSpec: pagesSpec, workers: workers, maxDim: maxDim, accurate: accurate)
} else if args.count >= 3 && args[1] == "--image" {
    let accurate = args.count >= 4 ? (args[3] != "0" && args[3].lowercased() != "false") : true
    handleOcrImage(path: args[2], accurate: accurate)
} else {
    fputs("Usage: vision_ocr_helper [--server | --image <path> [accurate] | --pdf-batch <pdf> <pages_csv> <workers> <max_dim> [accurate]]\n", stderr)
    exit(1)
}
