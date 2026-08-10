import SwiftUI

// MARK: - 数据模型（对齐后端 /memories 透传的 Ombre REST 形状）

/// 列表/搜索结果一条。字段全部可缺（搜索结果只有 id/name/score/preview 这几样），
/// 宽松解码——Ombre 升级加字段/改字段不至于让整页挂掉。
struct MemoryItem: Decodable, Identifiable {
    let id: String
    let name: String?
    let tags: [String]?
    let importance: Int?
    let pinned: Bool?
    let resolved: Bool?
    let created_epoch_ms: Int64?
    let last_active_epoch_ms: Int64?
    let content_preview: String?
    let dont_surface: Bool?        // 已遗忘（不主动浮现，但没抹掉）——决定按钮是「遗忘」还是「取消遗忘」

    var title: String { (name ?? "").isEmpty ? id : name! }
}

/// 详情：{id, metadata:{...}, content, display_content}。
struct MemoryDetail: Decodable {
    struct Meta: Decodable {
        let name: String?
        let tags: [String]?
        let importance: Int?
        let pinned: Bool?
        let resolved: Bool?
        let created: String?
        let dont_surface: Bool?
    }
    let id: String
    let metadata: Meta
    let content: String
    let display_content: String?

    var title: String { (metadata.name ?? "").isEmpty ? id : metadata.name! }
    var bodyText: String { display_content ?? content }
}

/// 改记忆的请求体（只发要改的字段；官方 /api/bucket/{id}/edit 的白名单子集）。
struct MemoryEdit: Encodable {
    var name: String? = nil
    var importance: Int? = nil
    var tags: [String]? = nil
    var content: String? = nil
    var pinned: Bool? = nil
    var resolved: Bool? = nil
}

/// 排序两档，单击切换（服务端排：created_desc / score）。
enum MemorySort: String {
    case created  = "created"    // 最新创建（默认）
    case activity = "activity"   // 活跃度分
    var label: String { self == .created ? "最新创建" : "活跃度分" }
    var icon: String { self == .created ? "calendar" : "waveform.path.ecg" }
    mutating func toggle() { self = self == .created ? .activity : .created }
}

/// epoch 毫秒 → 北京时间显示串。
private func beijingStamp(_ ms: Int64?) -> String? {
    guard let ms else { return nil }
    let f = DateFormatter()
    f.dateFormat = "yyyy/M/d HH:mm"
    f.timeZone = TimeZone(identifier: "Asia/Shanghai")
    return f.string(from: Date(timeIntervalSince1970: TimeInterval(ms) / 1000))
}

// MARK: - 后端调用

extension ChatService {
    func getMemories(sort: MemorySort) async throws -> [MemoryItem] {
        let data = try await perform(authedRequest("GET", "/memories?sort=\(sort.rawValue)"))
        do { return try JSONDecoder().decode([MemoryItem].self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    func searchMemories(_ q: String) async throws -> [MemoryItem] {
        let enc = q.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? q
        let data = try await perform(authedRequest("GET", "/memories?q=\(enc)"))
        do { return try JSONDecoder().decode([MemoryItem].self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    func getMemory(id: String) async throws -> MemoryDetail {
        let data = try await perform(authedRequest("GET", "/memories/\(id)"))
        do { return try JSONDecoder().decode(MemoryDetail.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    func editMemory(id: String, _ edit: MemoryEdit) async throws {
        let body = try JSONEncoder().encode(edit)
        _ = try await perform(authedRequest("POST", "/memories/\(id)/edit", jsonBody: body))
    }

    /// 主动遗忘开关（toggle）：调一次翻一次 dont_surface。返回翻转后的状态，供 app 立即对齐按钮文案。
    @discardableResult
    func forgetMemory(id: String) async throws -> Bool {
        let data = try await perform(authedRequest("POST", "/memories/\(id)/forget", jsonBody: Data("{}".utf8)))
        struct R: Decodable { let dont_surface: Bool? }
        return (try? JSONDecoder().decode(R.self, from: data))?.dont_surface ?? false
    }

    /// 归档：移进档案区、盖 deleted_at——从列表/搜索/回忆里都消失（比遗忘彻底，仍可在 Ombre 侧 restore）。
    func archiveMemory(id: String) async throws {
        _ = try await perform(authedRequest("POST", "/memories/\(id)/archive", jsonBody: Data("{}".utf8)))
    }
}

// MARK: - 记忆页（抽屉 → push 进来；返回走标准左上/左缘右滑）

struct MemoryPage: View {
    private let service = ChatService()

    @State private var memories: [MemoryItem] = []
    @State private var query = ""
    @State private var sort: MemorySort = .created
    @State private var loading = true
    @State private var errorText: String? = nil

    var body: some View {
        VStack(spacing: 0) {
            searchRow
            Divider()
            content
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle("记忆")
        .navigationBarTitleDisplayMode(.inline)
        .task { await reload() }
        .refreshable { await reload() }
    }

    /// 搜索行：左边排序切换按钮（单击翻转两档），右边搜索框（回车搜、清空回列表）。
    private var searchRow: some View {
        HStack(spacing: 10) {
            Button {
                sort.toggle()
                if query.isEmpty { Task { await reload() } }
            } label: {
                Label(sort.label, systemImage: sort.icon)
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(Color.theme)
                    .padding(.horizontal, 10)
                    .padding(.vertical, 7)
                    .background(Color.theme.opacity(0.12), in: Capsule())
            }
            .buttonStyle(.plain)
            TextField("搜索记忆", text: $query)
                .textFieldStyle(.plain)
                .submitLabel(.search)
                .onSubmit { Task { await runSearch() } }
                .onChange(of: query) { _, q in
                    if q.isEmpty { Task { await reload() } }
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .background(Color(.tertiarySystemFill), in: Capsule())
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.bar)
    }

    @ViewBuilder
    private var content: some View {
        if loading {
            ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if let errorText {
            ContentUnavailableView("读不到记忆", systemImage: "exclamationmark.triangle",
                                   description: Text(errorText))
        } else if memories.isEmpty {
            ContentUnavailableView(query.isEmpty ? "还没有长期记忆" : "没搜到",
                                   systemImage: "brain",
                                   description: Text(query.isEmpty ? "TA 还没往记忆里存东西。" : "换个词试试。"))
        } else {
            List {
                ForEach(memories) { m in
                    // 直连 NavigationLink：value-based 的 navigationDestination 声明在
                    // 已被 push 的页面里不触发（真机实测点了没反应），直给 destination 稳
                    NavigationLink {
                        MemoryDetailPage(id: m.id, fallbackTitle: m.title,
                                         createdMs: m.created_epoch_ms,
                                         onChanged: { Task { await reload() } })
                    } label: {
                        MemoryRow(item: m, sort: sort)
                    }
                    .swipeActions(edge: .trailing) {
                        // 归档：真正让它从列表/搜索/回忆里消失（红键）。
                        Button(role: .destructive) {
                            Task { try? await service.archiveMemory(id: m.id); await reload() }
                        } label: { Label("归档", systemImage: "archivebox") }
                        // 遗忘/取消遗忘：toggle，按钮文案本身就是当前状态（灰键，非破坏性）。
                        Button {
                            Task { try? await service.forgetMemory(id: m.id); await reload() }
                        } label: {
                            Label(m.dont_surface == true ? "取消遗忘" : "遗忘",
                                  systemImage: m.dont_surface == true ? "eye" : "eye.slash")
                        }
                        .tint(.gray)
                    }
                }
            }
            .listStyle(.plain)
        }
    }

    private func reload() async {
        guard query.isEmpty else { return await runSearch() }
        loading = memories.isEmpty
        errorText = nil
        do { memories = try await service.getMemories(sort: sort) }
        catch { errorText = (error as? ChatServiceError)?.errorDescription ?? "记忆服务不在线" }
        loading = false
    }

    private func runSearch() async {
        let q = query.trimmingCharacters(in: .whitespaces)
        guard !q.isEmpty else { return await reload() }
        loading = true
        errorText = nil
        do { memories = try await service.searchMemories(q) }
        catch { errorText = (error as? ChatServiceError)?.errorDescription ?? "搜索失败" }
        loading = false
    }
}

// MARK: - 列表一行

private struct MemoryRow: View {
    let item: MemoryItem
    let sort: MemorySort

    private var stamp: String? {
        sort == .created ? beijingStamp(item.created_epoch_ms).map { "建于 \($0)" }
                         : beijingStamp(item.last_active_epoch_ms).map { "活跃 \($0)" }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                if item.pinned == true {
                    Image(systemName: "pin.fill").font(.caption2).foregroundStyle(Color.theme)
                }
                Text(item.title).font(.callout.weight(.medium)).lineLimit(1)
                if item.dont_surface == true {
                    Image(systemName: "eye.slash").font(.caption2).foregroundStyle(.tertiary)
                }
                Spacer()
                if let imp = item.importance {
                    Text("★\(imp)").font(.caption2).foregroundStyle(.secondary)
                }
            }
            if let p = item.content_preview, !p.isEmpty {
                Text(p).font(.footnote).foregroundStyle(.secondary).lineLimit(2)
            }
            HStack(spacing: 6) {
                if let tags = item.tags, !tags.isEmpty {
                    Text(tags.prefix(4).map { "#\($0)" }.joined(separator: " "))
                        .font(.caption2).foregroundStyle(.tertiary).lineLimit(1)
                }
                Spacer(minLength: 4)
                if let stamp {
                    Text(stamp).font(.caption2).foregroundStyle(.tertiary)
                }
            }
        }
        .padding(.vertical, 4)
        .opacity(item.resolved == true ? 0.5 : 1)   // 已沉底的淡一点
    }
}

// MARK: - 详情（push 页：左上标准返回，右上编辑）

private struct MemoryDetailPage: View {
    let id: String
    let fallbackTitle: String
    var createdMs: Int64? = nil   // 列表带来的创建时间（搜索结果没有 → 退回原字符串）
    var onChanged: () -> Void
    private let service = ChatService()
    @Environment(\.dismiss) private var dismiss

    @State private var detail: MemoryDetail? = nil
    @State private var loading = true
    @State private var showEdit = false

    var body: some View {
        Group {
            if loading {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let d = detail {
                ScrollView {
                    VStack(alignment: .leading, spacing: 12) {
                        HStack(spacing: 12) {
                            if d.metadata.pinned == true {
                                Label("置顶", systemImage: "pin.fill").foregroundStyle(Color.theme)
                            }
                            if let imp = d.metadata.importance { Text("★ \(imp)") }
                            // 时间优先用 epoch 转北京；搜索进来的没有 epoch → 退回原字符串
                            // （Ombre 侧是本地时区裸字符串，只作兜底展示）
                            if let stamp = beijingStamp(createdMs) {
                                Text(stamp).foregroundStyle(.secondary)
                            } else if let c = d.metadata.created, !c.isEmpty {
                                Text(c.prefix(16).replacingOccurrences(of: "T", with: " "))
                                    .foregroundStyle(.secondary)
                            }
                            Spacer()
                            if d.metadata.resolved == true { Text("已沉底").foregroundStyle(.secondary) }
                        }
                        .font(.caption)
                        if let tags = d.metadata.tags, !tags.isEmpty {
                            Text(tags.map { "#\($0)" }.joined(separator: "  "))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                        Divider()
                        Text(d.bodyText).font(.body).textSelection(.enabled)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                    .padding()
                }
            } else {
                ContentUnavailableView("读不到这条", systemImage: "exclamationmark.triangle")
            }
        }
        .navigationTitle(detail?.title ?? fallbackTitle)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { showEdit = true } label: { Image(systemName: "pencil") }
                    .disabled(detail == nil)
            }
        }
        .sheet(isPresented: $showEdit) {
            if let d = detail {
                EditMemorySheet(detail: d,
                                onSave: { edit in Task { await apply(edit) } },
                                onArchive: { Task { await archiveSelf() } },
                                onForget: { Task { await forgetSelf() } })
            }
        }
        .task { await load() }
    }

    private func load() async {
        detail = try? await service.getMemory(id: id)
        loading = false
    }

    private func apply(_ edit: MemoryEdit) async {
        try? await service.editMemory(id: id, edit)
        await load()
        onChanged()
    }

    /// 归档：从列表/搜索/回忆里移走（红键），归档完这条详情页没意义了 → 退出。
    private func archiveSelf() async {
        try? await service.archiveMemory(id: id)
        onChanged()
        dismiss()
    }

    /// 遗忘 toggle：翻转 dont_surface，留在原地，刷新详情让状态跟上（不退出）。
    private func forgetSelf() async {
        try? await service.forgetMemory(id: id)
        await load()
        onChanged()
    }
}

// MARK: - 编辑（标题/重要度/标签/正文 + 置顶/沉底 + 删除）

private struct EditMemorySheet: View {
    let detail: MemoryDetail
    let onSave: (MemoryEdit) -> Void
    let onArchive: () -> Void
    let onForget: () -> Void
    @Environment(\.dismiss) private var dismiss

    @State private var name: String
    @State private var importance: Int
    @State private var tagsText: String
    @State private var content: String
    @State private var pinned: Bool
    @State private var resolved: Bool
    @State private var forgotten: Bool
    @State private var confirmArchive = false

    init(detail: MemoryDetail, onSave: @escaping (MemoryEdit) -> Void,
         onArchive: @escaping () -> Void, onForget: @escaping () -> Void) {
        self.detail = detail
        self.onSave = onSave
        self.onArchive = onArchive
        self.onForget = onForget
        _name = State(initialValue: detail.metadata.name ?? "")
        _importance = State(initialValue: detail.metadata.importance ?? 5)
        _tagsText = State(initialValue: (detail.metadata.tags ?? []).joined(separator: ", "))
        _content = State(initialValue: detail.content)
        _pinned = State(initialValue: detail.metadata.pinned ?? false)
        _resolved = State(initialValue: detail.metadata.resolved ?? false)
        _forgotten = State(initialValue: detail.metadata.dont_surface ?? false)
    }

    var body: some View {
        NavigationStack {
            Form {
                Section("标题") {
                    TextField("标题", text: $name)
                }
                Section("重要度") {
                    Stepper("★ \(importance)", value: $importance, in: 1...10)
                }
                Section("标签（逗号分隔）") {
                    TextField("tag1, tag2", text: $tagsText, axis: .vertical)
                }
                Section("正文") {
                    TextField("正文", text: $content, axis: .vertical)
                        .lineLimit(6...20)
                }
                Section {
                    Toggle("置顶（不衰减）", isOn: $pinned)
                    Toggle("沉底（标记已解决）", isOn: $resolved)
                    // 遗忘：翻转即刻生效（走 toggle 端点），不随「保存」走。
                    Toggle("遗忘（不再主动浮现，仍可搜到）", isOn: $forgotten)
                        .onChange(of: forgotten) { _, _ in onForget() }
                }
                Section {
                    Button("归档这条记忆", role: .destructive) { confirmArchive = true }
                } footer: {
                    Text("归档＝从列表、搜索、回忆里都移走（比遗忘彻底）。Ombre 不做物理删除，日后仍可恢复。")
                }
            }
            .navigationTitle("编辑记忆")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button { dismiss() } label: { Image(systemName: "xmark") }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button { save() } label: { Image(systemName: "checkmark") }
                }
            }
            .confirmationDialog("归档这条记忆？会从列表、搜索、回忆里移走（日后仍可恢复）。",
                                isPresented: $confirmArchive, titleVisibility: .visible) {
                Button("归档", role: .destructive) { onArchive(); dismiss() }
                Button("取消", role: .cancel) { }
            }
        }
    }

    /// 只发真正改了的字段（空标题/空正文不发，避免误清）。
    private func save() {
        var e = MemoryEdit()
        let n = name.trimmingCharacters(in: .whitespaces)
        if !n.isEmpty && n != (detail.metadata.name ?? "") { e.name = n }
        if importance != (detail.metadata.importance ?? 5) { e.importance = importance }
        let tags = tagsText.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        if tags != (detail.metadata.tags ?? []) { e.tags = tags }
        let c = content.trimmingCharacters(in: .whitespacesAndNewlines)
        if !c.isEmpty && c != detail.content { e.content = c }
        if pinned != (detail.metadata.pinned ?? false) { e.pinned = pinned }
        if resolved != (detail.metadata.resolved ?? false) { e.resolved = resolved }
        // 什么都没改就直接关，别打一个必 400 的空请求
        if e.name != nil || e.importance != nil || e.tags != nil || e.content != nil
            || e.pinned != nil || e.resolved != nil {
            onSave(e)
        }
        dismiss()
    }
}
