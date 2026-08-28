import UIKit

/// 输入区里还没发出去的东西：文字 + 待发照片 + 待发文件 + 待发表情。
struct ComposeDraft {
    var text: String = ""
    var images: [Data] = []
    var files: [OutgoingFile] = []
    var stickerID: String? = nil

    var isEmpty: Bool { text.isEmpty && images.isEmpty && files.isEmpty && stickerID == nil }
}

/// 草稿仓：**一人一份**，落 `Documents/drafts/<charID>/`（meta.json 记文字和清单，
/// 图/文件各自落盘），app 重启不丢。
///
/// 为什么要按角色分：草稿是「说给某个人听的话」。以前它是 ContentView 的 @State、
/// 切会话不清空——给 A 打了一半的字、给 A 选好的图，切到 B 按发送就发给 B 了。
/// 这是 2026-08-28 复盘那个坑（延迟写入 + 可变的全局当前）扫出来的同类。
///
/// 所以这里从一开始就按那次的规矩来：
///   - 每次存都要显式说**存给谁**，不在存的那一刻现问「现在是谁」；
///   - 文字防抖 500ms 才落盘（每敲一字写一次盘太蠢），(值, 角色) 在**排期那一刻**
///     成对快照——防抖期间切人，这一份照样存回它原本那位；
///   - 切人前 flushPending()，上一位没落盘的先写下去。
///
/// 存盘策略分两档：文字变化只重写 meta.json（附件是大块，别跟着每个字重写）；
/// 附件增删走 blobs: true 整份重写。空草稿 = 直接删目录，不留空壳。
@MainActor
final class DraftStore: ObservableObject {

    private let fm = FileManager.default
    private var saveTask: Task<Void, Never>? = nil
    /// 已排期、还没落盘的那一份（角色 + 值）。
    private var pending: (char: String, draft: ComposeDraft)? = nil

    private var root: URL {
        AppFiles.documents.appendingPathComponent("drafts", isDirectory: true)
    }

    private func dir(_ char: String) -> URL {
        root.appendingPathComponent(char, isDirectory: true)
    }

    // MARK: - 盘上的样子

    private struct FileMeta: Codable {
        let file: String     // 落盘文件名
        let name: String     // 原始文件名（发送时要用）
        let mime: String
    }

    private struct Meta: Codable {
        var text: String = ""
        var stickerID: String? = nil
        var images: [String] = []
        var files: [FileMeta] = []
    }

    // MARK: - 读

    /// 读某个角色的草稿（没有 = 空草稿）。
    func load(_ char: String) -> ComposeDraft {
        let d = dir(char)
        guard let data = try? Data(contentsOf: d.appendingPathComponent("meta.json")),
              let meta = try? JSONDecoder().decode(Meta.self, from: data) else {
            return ComposeDraft()
        }
        var draft = ComposeDraft(text: meta.text)
        draft.stickerID = meta.stickerID
        // 单个附件文件丢了（手动清过沙盒等）只丢那一个，别让整份草稿跟着报废。
        draft.images = meta.images.compactMap { try? Data(contentsOf: d.appendingPathComponent($0)) }
        draft.files = meta.files.compactMap { f in
            guard let bytes = try? Data(contentsOf: d.appendingPathComponent(f.file)) else { return nil }
            return OutgoingFile(data: bytes, name: f.name, mime: f.mime)
        }
        return draft
    }

    // MARK: - 写

    /// 立刻落盘。blobs=true 时连附件一起重写（附件增删走这条）；false 只重写 meta.json。
    func save(_ draft: ComposeDraft, char: String, blobs: Bool = true) {
        saveTask?.cancel()
        if pending?.char == char { pending = nil }
        writeNow(draft, char: char, blobs: blobs)
    }

    /// 文字改动：500ms 防抖后落盘（只重写 meta.json）。
    /// 快照连角色一起捕获——防抖期间切人，这一份照样存回它本来那位。
    func scheduleSave(_ draft: ComposeDraft, char: String, debounceMs: Int = 500) {
        // 排期中的那份属于别人 → 先把它写下去，再排这一份（别互相顶掉）。
        if let p = pending, p.char != char { flushPending() }
        let snapshot = draft, id = char
        pending = (id, snapshot)
        saveTask?.cancel()
        saveTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(debounceMs))
            guard !Task.isCancelled, let self else { return }
            self.writeNow(snapshot, char: id, blobs: false)
            if self.pending?.char == id { self.pending = nil }
        }
    }

    /// 把排期中的那份立刻落盘（切人前 / 退到后台时用）。
    func flushPending() {
        saveTask?.cancel()
        guard let p = pending else { return }
        pending = nil
        writeNow(p.draft, char: p.char, blobs: false)
    }

    /// 清掉某个角色的草稿（发送成功后）。
    func clear(_ char: String) {
        saveTask?.cancel()
        if pending?.char == char { pending = nil }
        try? fm.removeItem(at: dir(char))
    }

    // MARK: - 私有

    private func writeNow(_ draft: ComposeDraft, char: String, blobs: Bool) {
        let d = dir(char)
        guard !draft.isEmpty else {
            try? fm.removeItem(at: d)   // 空草稿不留空壳目录
            return
        }
        try? fm.createDirectory(at: d, withIntermediateDirectories: true)
        var meta = Meta(text: draft.text, stickerID: draft.stickerID)
        if blobs {
            // 整份重写：先扫掉旧附件，再按顺序写新的（名字按下标定，和 meta 一一对应）。
            for old in (try? fm.contentsOfDirectory(atPath: d.path)) ?? [] where old != "meta.json" {
                try? fm.removeItem(at: d.appendingPathComponent(old))
            }
            for (i, img) in draft.images.enumerated() {
                let name = "img_\(i).jpg"
                do {
                    try img.write(to: d.appendingPathComponent(name), options: .atomic)
                    meta.images.append(name)
                } catch { continue }   // 写不下去的那一张就不进清单，别记一个取不出来的名字
            }
            for (i, f) in draft.files.enumerated() {
                let name = "file_\(i)"
                do {
                    try f.data.write(to: d.appendingPathComponent(name), options: .atomic)
                    meta.files.append(FileMeta(file: name, name: f.name, mime: f.mime))
                } catch { continue }
            }
        } else {
            // 只改文字：附件没动，沿用盘上已有的清单（重读一次比重写几 MB 便宜得多）。
            if let data = try? Data(contentsOf: d.appendingPathComponent("meta.json")),
               let old = try? JSONDecoder().decode(Meta.self, from: data) {
                meta.images = old.images
                meta.files = old.files
            }
        }
        if let data = try? JSONEncoder().encode(meta) {
            try? data.write(to: d.appendingPathComponent("meta.json"), options: .atomic)
        }
    }
}
