import SwiftUI

// MARK: - 数据模型（对齐后端 /prs、/prs/{id}、/admin/restart，PLAN_prs §5/§6）

struct PRRow: Decodable, Identifiable, Equatable {
    let id: Int
    let char: String
    let char_name: String?
    let title: String
    let status: String            // open / reverted / merged / closed
    let updated_at: String?
    let touches_ios: Bool?
    let tests_ok: Bool?
    let effective: Bool?          // 合并了的此刻生没生效（nil = 不是合并态）
}

/// 派出去的活（PLAN_delegate §5）：跑着 / 等回答的列在页顶；收口的只给今天的。
struct DelegateRow: Decodable, Identifiable, Equatable {
    struct QA: Decodable, Equatable { let q: String; let a: String; let ts: String? }
    let char: String
    let char_name: String?
    let slug: String
    let model: String
    let status: String            // running / asking / opened / failed / cancelled
    let pr_id: Int?
    let question: String?
    let error: String?
    let started: String?
    let ended: String?
    let qa: [QA]?
    let brief: String?
    var id: String { char + "~" + slug }
}

/// PR 记录里的「派来的」信息（详情页展开说明原文 + 问答来回）。
struct PRDelegated: Decodable {
    struct QA: Decodable { let q: String; let a: String; let ts: String? }
    let model: String?
    let slug: String?
    let brief: String?
    let qa: [QA]?
}

struct PRListWrap: Decodable {
    let prs: [PRRow]
    let delegates: [DelegateRow]
    let unapplied: Int            // 合并了还没生效的个数

    enum CodingKeys: String, CodingKey { case prs, delegates, unapplied }
    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        prs = try c.decode([PRRow].self, forKey: .prs)
        delegates = (try? c.decode([DelegateRow].self, forKey: .delegates)) ?? []   // 老后端没有
        unapplied = try c.decode(Int.self, forKey: .unapplied)
    }
}

struct PRFile: Decodable, Hashable {
    let path: String
    let add: Int?                 // 二进制文件 nil
    let del: Int?
}

struct PRTests: Decodable {
    let ok: Bool?
    let summary: String?
}

struct PRDetail: Decodable, Identifiable {
    let id: Int
    let char: String
    let char_name: String?
    let title: String
    let description: String?
    let status: String
    let files: [PRFile]?
    let touches_ios: Bool?
    let tests: PRTests?
    let created_at: String?
    let updated_at: String?
    let merged_at: String?
    let revert_reason: String?
    let close_reason: String?
    let closed_by: String?
    let forced: Bool?
    let diff: String?
    let effective: Bool?
    let blockers: [String]?
    let delegated: PRDelegated?   // 派来的 PR 带：模型 + 说明原文 + 问答来回
}

struct RestartPreview: Decodable {
    struct Pending: Decodable { let id: Int; let title: String }
    let running: [String]
    let pending: [Pending]
    let blockers: [String]
}

private struct RestartAccepted: Decodable {
    let old_pid: Int
    let reverts_if_fail: [Int]?
}

private struct HealthPID: Decodable { let pid: Int? }

/// PR 的写操作被挡（409）：后端 detail 是个对象 {error, needs_force?, tests?}，
/// 通用 perform 只认字符串 detail，这里自己拆。
enum PRActionError: LocalizedError {
    case refused(String)
    case needsForce(String)       // 测试没过：弹「取消 / 强行合并」

    var errorDescription: String? {
        switch self {
        case .refused(let m):    return m
        case .needsForce(let m): return m
        }
    }
}

extension ChatService {
    func getPRs() async throws -> PRListWrap {
        let data = try await perform(authedRequest("GET", "/prs"))
        do { return try JSONDecoder().decode(PRListWrap.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    func getPR(_ id: Int) async throws -> PRDetail {
        let data = try await perform(authedRequest("GET", "/prs/\(id)", timeout: 30))
        do { return try JSONDecoder().decode(PRDetail.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    func mergePR(_ id: Int, force: Bool) async throws {
        let body = try JSONSerialization.data(withJSONObject: ["force": force])
        _ = try await prAction(authedRequest("POST", "/prs/\(id)/merge", jsonBody: body, timeout: 60))
    }

    func closePR(_ id: Int, reason: String) async throws {
        let body = try JSONSerialization.data(withJSONObject: ["reason": reason])
        _ = try await prAction(authedRequest("POST", "/prs/\(id)/close", jsonBody: body))
    }

    /// 撤回一个派出去的活（跑着的掐掉 / 等回答的收掉）。409 = 已经收口了。
    func cancelDelegate(char: String, slug: String) async throws {
        let body = try JSONSerialization.data(withJSONObject: ["char": char, "slug": slug,
                                                               "reason": "机主在 PR 页撤回"])
        _ = try await prAction(authedRequest("POST", "/prs/delegate/cancel", jsonBody: body))
    }

    func getRestartPreview() async throws -> RestartPreview {
        let data = try await perform(authedRequest("GET", "/admin/restart", timeout: 30))
        do { return try JSONDecoder().decode(RestartPreview.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    /// 按下重启：后端先预检（import 新代码，要几秒）再起看守，回旧进程的 pid。
    func requestRestart() async throws -> Int {
        let body = try JSONSerialization.data(withJSONObject: ["confirm": true])
        let data = try await prAction(authedRequest("POST", "/admin/restart", jsonBody: body,
                                                    timeout: 150))
        guard let r = try? JSONDecoder().decode(RestartAccepted.self, from: data)
        else { throw ChatServiceError.badResponse }
        return r.old_pid
    }

    /// 后端此刻的进程号；连不上 = nil（重启中间有一段就是连不上，不算错）。
    func healthPID() async -> Int? {
        guard let req = try? authedRequest("GET", "/health", timeout: 3),
              let (data, _) = try? await URLSession.shared.data(for: req) else { return nil }
        return (try? JSONDecoder().decode(HealthPID.self, from: data))?.pid
    }

    private func prAction(_ request: URLRequest) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch let error as URLError {
            throw error.code == .timedOut ? ChatServiceError.timedOut : ChatServiceError.cannotConnect
        }
        guard let http = response as? HTTPURLResponse else { throw ChatServiceError.badResponse }
        guard (200..<300).contains(http.statusCode) else {
            let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            if let d = obj?["detail"] as? [String: Any] {
                let msg = d["error"] as? String ?? "HTTP \(http.statusCode)"
                if d["needs_force"] as? Bool == true {
                    throw PRActionError.needsForce(d["tests"] as? String ?? msg)
                }
                throw PRActionError.refused(msg)
            }
            throw ChatServiceError.server(obj?["detail"] as? String ?? "HTTP \(http.statusCode)")
        }
        return data
    }
}

// MARK: - 状态的样子（§5：角色看圆点，状态看左侧细条 + 墨色浓淡；不铺底色）

private enum PRLook {
    static let amber = Color(hex: 0xC98A2E)

    static func label(_ status: String) -> String {
        switch status {
        case "open":     return "待合并"
        case "reverted": return "已退回"
        case "merged":   return "已合并"
        case "closed":   return "已关闭"
        default:         return status
        }
    }

    /// 左侧 2pt 细条：待合并墨色实心 / 已退回琥珀 / 已合并浅灰 / 已关闭深灰（机主原话落在细条上）
    static func bar(_ status: String) -> Color {
        switch status {
        case "open":     return .primary
        case "reverted": return amber
        case "merged":   return Color(.systemGray4)
        default:         return Color(.systemGray)
        }
    }

    /// 标题墨色浓度：关掉的最淡——最不需要再看
    static func titleOpacity(_ status: String) -> Double {
        switch status {
        case "merged": return 0.55
        case "closed": return 0.35
        default:       return 1
        }
    }

    /// 其余文字：没办完的走次级墨色（nil），收口的按表取浓度
    static func restOpacity(_ status: String) -> Double? {
        switch status {
        case "merged": return 0.40
        case "closed": return 0.30
        default:       return nil
        }
    }

    static func unfinished(_ status: String) -> Bool { status == "open" || status == "reverted" }

    /// "2026-09-24T10:40:52+08:00" → "09-24 10:40"
    static func time(_ iso: String?) -> String {
        guard let iso else { return "" }
        let s = String(iso.prefix(16)).replacingOccurrences(of: "T", with: " ")
        return s.count > 5 ? String(s.dropFirst(5)) : s
    }
}

private struct RestText: ViewModifier {
    let status: String
    func body(content: Content) -> some View {
        if let o = PRLook.restOpacity(status) {
            content.foregroundStyle(Color.primary.opacity(o))
        } else {
            content.foregroundStyle(.secondary)
        }
    }
}

// MARK: - 页顶：生效状态 + 重启后端（§6.2）

/// 重启是整台后端的事，列表页和详情页顶上都挂这一条。按下 → 拉确认数据（会掐断什么、
/// 随这次生效哪几个 PR）→ 确认 → 后端预检 + 起看守回 202 → 这里轮询 /health 等 pid 变。
/// 起不来时看守会自动退回并走 Bark，这边只负责等和说清楚。
struct RestartBar: View {
    let unapplied: Int
    var onRestarted: () async -> Void = { }

    private let service = ChatService()
    @State private var preview: RestartPreview? = nil
    @State private var loadingPreview = false
    @State private var phase: Phase = .idle
    @State private var errorText: String? = nil

    enum Phase: Equatable { case idle, preflight, waiting, done, timedOut }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(statusText)
                .font(.footnote)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 8)
            if phase == .preflight || phase == .waiting || loadingPreview {
                ProgressView().controlSize(.small)
            } else {
                Button("重启后端") { Task { await openConfirm() } }
                    .font(.footnote.weight(.semibold))
                    .foregroundStyle(Color.userAccent)
            }
        }
        .alert(confirmTitle, isPresented: Binding(
            get: { preview != nil }, set: { if !$0 { preview = nil } }
        )) {
            if preview?.blockers.isEmpty ?? true {
                Button("取消", role: .cancel) { }
                Button("重启") { Task { await restart() } }
            } else {
                Button("好", role: .cancel) { }
            }
        } message: {
            Text(confirmMessage)
        }
        .alert("重启没成", isPresented: Binding(
            get: { errorText != nil }, set: { if !$0 { errorText = nil } }
        )) { Button("好", role: .cancel) { } } message: { Text(errorText ?? "") }
    }

    private var statusText: String {
        switch phase {
        case .preflight: return "正在检查新代码能不能起来…"
        case .waiting:   return "重启中，后端这会儿连不上是正常的…"
        case .done:      return "后端已重启"
        case .timedOut:  return "等了两分半还没回来——结果会推到 Bark；也可能起不来已被自动退回"
        case .idle:
            return unapplied > 0
                ? "已合并 \(unapplied) 个，还没生效（后端跑的是合并前的代码）"
                : "后端跑的就是最新合并的代码"
        }
    }

    private var confirmTitle: String {
        guard let p = preview else { return "" }
        return p.blockers.isEmpty ? "重启后端？" : "现在不能重启"
    }

    private var confirmMessage: String {
        guard let p = preview else { return "" }
        if !p.blockers.isEmpty { return p.blockers.joined(separator: "\n") }
        var parts: [String] = []
        parts.append(p.running.isEmpty ? "现在没有在跑的东西。"
                     : "会掐断：\n" + p.running.map { "· " + $0 }.joined(separator: "\n"))
        if !p.pending.isEmpty {
            parts.append("随这次生效：\n" + p.pending.map { "· #\($0.id) \($0.title)" }
                .joined(separator: "\n")
                + "\n起不来的话这几个会被自动退回，后端用退回后的代码再起。")
        }
        return parts.joined(separator: "\n\n")
    }

    private func openConfirm() async {
        loadingPreview = true
        defer { loadingPreview = false }
        do { preview = try await service.getRestartPreview() }
        catch { errorText = error.localizedDescription }
    }

    private func restart() async {
        phase = .preflight
        let oldPID: Int
        do { oldPID = try await service.requestRestart() }
        catch {
            phase = .idle
            errorText = error.localizedDescription
            return
        }
        phase = .waiting
        // 看守：停旧的最多 25s + 验活最多 60s，起不来还要退回再起一轮——等 150s
        let deadline = Date().addingTimeInterval(150)
        try? await Task.sleep(for: .seconds(3))
        while Date() < deadline {
            if let pid = await service.healthPID(), pid != oldPID {
                phase = .done
                await onRestarted()
                return
            }
            try? await Task.sleep(for: .seconds(2))
        }
        phase = .timedOut
    }
}

// MARK: - 列表页（抽屉 → PR）

struct PRsPage: View {
    @Binding var openID: Int?                          // 聊天里点系统行跳进来时直接打开这一个
    var onAsk: (String, String) -> Void = { _, _ in }  // 问问他：(角色 id, 预填的话)

    private let service = ChatService()
    @State private var wrap: PRListWrap? = nil
    @State private var loading = true
    @State private var errorText: String? = nil

    var body: some View {
        Group {
            if loading && wrap == nil {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorText, wrap == nil {
                ContentUnavailableView("读不到 PR", systemImage: "exclamationmark.triangle",
                                       description: Text(errorText))
            } else {
                List {
                    Section {
                        RestartBar(unapplied: wrap?.unapplied ?? 0) { await load() }
                    }
                    // 派活（PLAN_delegate §5）：跑着 / 等回答的列在页顶，可撤回
                    let dele = wrap?.delegates ?? []
                    if !dele.isEmpty {
                        Section("派出去的活") { ForEach(dele) { delegateRow($0) } }
                    }
                    let rows = wrap?.prs ?? []
                    if rows.isEmpty {
                        Section {
                            Text("还没有 PR。他醒着想改代码时，会在自己的工作副本里改完提到这儿。")
                                .font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                    // 没办完的一组、收口的一组（后端已按组内时间倒序排好）
                    let open = rows.filter { PRLook.unfinished($0.status) }
                    let done = rows.filter { !PRLook.unfinished($0.status) }
                    if !open.isEmpty { Section { ForEach(open) { row($0) } } }
                    if !done.isEmpty { Section { ForEach(done) { row($0) } } }
                }
                .listStyle(.insetGrouped)
            }
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle("PR")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
        .navigationDestination(item: $openID) { id in
            PRDetailPage(id: id, onAsk: onAsk, onChanged: { await load() })
        }
    }

    private func row(_ r: PRRow) -> some View {
        Button { openID = r.id } label: {
            HStack(spacing: 10) {
                Rectangle()
                    .fill(PRLook.bar(r.status))
                    .frame(width: 2)
                    .padding(.vertical, 2)
                Circle()
                    .fill(IdentityColor.color(for: r.char))
                    .frame(width: 6, height: 6)
                    .opacity(PRLook.titleOpacity(r.status))
                Text(r.title)
                    .font(.subheadline.weight(r.status == "open" ? .semibold : .regular))
                    .foregroundStyle(Color.primary.opacity(PRLook.titleOpacity(r.status)))
                    .lineLimit(2)
                Spacer(minLength: 6)
                VStack(alignment: .trailing, spacing: 2) {
                    Text(PRLook.label(r.status))
                    Text(PRLook.time(r.updated_at))
                }
                .font(.caption)
                .modifier(RestText(status: r.status))
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func delegateRow(_ d: DelegateRow) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Circle().fill(IdentityColor.color(for: d.char)).frame(width: 6, height: 6)
                Text("\(d.char_name ?? d.char) 派 \(d.model)")
                Text("·")
                Text(d.slug).font(.caption.monospaced())
                Spacer()
                Text(delegateStatus(d))
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            if d.status == "asking", let q = d.question, !q.isEmpty {
                Text(q).font(.footnote).lineLimit(4)
            } else if d.status == "failed", let e = d.error, !e.isEmpty {
                Text(e).font(.footnote).foregroundStyle(PRLook.amber).lineLimit(3)
            } else if let b = d.brief, !b.isEmpty {
                Text(b).font(.footnote).foregroundStyle(.secondary).lineLimit(2)
            }
            if d.status == "running" || d.status == "asking" {
                Button("撤回", role: .destructive) {
                    Task {
                        try? await service.cancelDelegate(char: d.char, slug: d.slug)
                        await load()
                    }
                }
                .font(.caption)
            }
        }
        .padding(.vertical, 2)
    }

    private func delegateStatus(_ d: DelegateRow) -> String {
        switch d.status {
        case "running":   return "跑了 \(minutesSince(d.started)) 分钟"
        case "asking":    return "等他回答"
        case "opened":    return d.pr_id.map { "提了 #\($0)" } ?? "提了"
        case "failed":    return "没做成"
        case "cancelled": return "撤回了"
        default:          return d.status
        }
    }

    private func minutesSince(_ iso: String?) -> Int {
        guard let iso, let d = ISO8601DateFormatter().date(from: iso) else { return 0 }
        return max(0, Int(Date().timeIntervalSince(d) / 60))
    }

    private func load() async {
        do {
            wrap = try await service.getPRs()
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
        loading = false
    }
}

// MARK: - 详情页

struct PRDetailPage: View {
    let id: Int
    var onAsk: (String, String) -> Void = { _, _ in }
    var onChanged: () async -> Void = { }

    private let service = ChatService()
    @State private var pr: PRDetail? = nil
    @State private var errorText: String? = nil
    @State private var busy = false
    @State private var testsOpen = false
    @State private var confirmMerge = false
    @State private var forceText: String? = nil        // 测试没过：弹「取消 / 强行合并」
    @State private var closing = false
    @State private var closeReason = ""
    @State private var opError: String? = nil
    @State private var unapplied = 0

    var body: some View {
        Group {
            if let pr {
                content(pr)
            } else if let errorText {
                ContentUnavailableView("读不到这个 PR", systemImage: "exclamationmark.triangle",
                                       description: Text(errorText))
            } else {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle("#\(id)")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
        .confirmationDialog("把 #\(id) 合进主目录？只在本机，不推送；合完要重启后端才生效。",
                            isPresented: $confirmMerge, titleVisibility: .visible) {
            Button("合并") { Task { await merge(force: false) } }
            Button("取消", role: .cancel) { }
        }
        .alert("测试没过", isPresented: Binding(
            get: { forceText != nil }, set: { if !$0 { forceText = nil } }
        )) {
            Button("取消", role: .cancel) { }
            Button("强行合并", role: .destructive) { Task { await merge(force: true) } }
        } message: {
            Text(forceText ?? "")
        }
        .alert("关掉 #\(id)？", isPresented: $closing) {
            TextField("为什么关（可以不写）", text: $closeReason)
            Button("取消", role: .cancel) { }
            Button("关闭", role: .destructive) { Task { await close() } }
        } message: {
            Text("他下一轮会知道被关了；分支 7 天后清掉，记录一直留着。")
        }
        .alert("没办成", isPresented: Binding(
            get: { opError != nil }, set: { if !$0 { opError = nil } }
        )) { Button("好", role: .cancel) { } } message: { Text(opError ?? "") }
    }

    @ViewBuilder
    private func content(_ pr: PRDetail) -> some View {
        List {
            if unapplied > 0 {
                Section { RestartBar(unapplied: unapplied) { await load() } }
            }

            // 1. 人话描述（最上面，大字）
            Section {
                VStack(alignment: .leading, spacing: 10) {
                    HStack(spacing: 6) {
                        Circle().fill(IdentityColor.color(for: pr.char)).frame(width: 6, height: 6)
                        Text(pr.char_name ?? pr.char)
                        Text("·")
                        Text(statusLine(pr))
                        Spacer()
                        Text(PRLook.time(pr.updated_at))
                    }
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    Text(pr.title)
                        .font(.subheadline.weight(.semibold))
                    Text(pr.description ?? "")
                        .font(.body)
                        .textSelection(.enabled)
                }
                .padding(.vertical, 4)
            }

            // 派来的 PR（PLAN_delegate §5）：标签 + 说明原文 + 问答来回
            if let dl = pr.delegated, let model = dl.model {
                Section("\(pr.char_name ?? pr.char) 派 \(model) 做的") {
                    if let b = dl.brief, !b.isEmpty {
                        DisclosureGroup("他写的说明") {
                            Text(b).font(.footnote).textSelection(.enabled)
                        }
                        .font(.subheadline)
                    }
                    ForEach(Array((dl.qa ?? []).enumerated()), id: \.offset) { _, qa in
                        VStack(alignment: .leading, spacing: 3) {
                            Text("问：" + qa.q).font(.footnote)
                            Text("答：" + qa.a).font(.footnote).foregroundStyle(.secondary)
                        }
                    }
                }
            }

            if pr.status == "reverted", let why = pr.revert_reason, !why.isEmpty {
                Section("为什么被退回") {
                    Text(why)
                        .font(.caption.monospaced())
                        .textSelection(.enabled)
                }
            }
            if pr.status == "closed", let why = pr.close_reason, !why.isEmpty {
                Section("关掉的原因") { Text(why).font(.footnote) }
            }
            if pr.status == "open", let b = pr.blockers, !b.isEmpty {
                Section("现在合不了") {
                    ForEach(b, id: \.self) { Text($0).font(.footnote).foregroundStyle(PRLook.amber) }
                }
            }

            // 2. 测试结果
            Section {
                Button { testsOpen.toggle() } label: {
                    HStack {
                        Text(testsHeadline(pr))
                        Spacer()
                        Image(systemName: "chevron.right")
                            .font(.caption.weight(.semibold))
                            .rotationEffect(.degrees(testsOpen ? 90 : 0))
                            .foregroundStyle(.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                if testsOpen {
                    Text(pr.tests?.summary ?? "没有测试结果")
                        .font(.caption.monospaced())
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }

            // 3. 改了哪些文件
            Section {
                if pr.touches_ios == true {
                    Text("⚠️ 改了 app，合并后要重新装包才看得到")
                        .font(.footnote)
                        .foregroundStyle(PRLook.amber)
                }
                ForEach(pr.files ?? [], id: \.self) { f in
                    HStack(alignment: .firstTextBaseline) {
                        Text(f.path)
                            .font(.caption.monospaced())
                            .lineLimit(2)
                            .truncationMode(.head)
                        Spacer(minLength: 8)
                        if let a = f.add, let d = f.del {
                            Text("+\(a)").foregroundStyle(.green)
                            Text("−\(d)").foregroundStyle(.red)
                        } else {
                            Text("二进制").foregroundStyle(.secondary)
                        }
                    }
                    .font(.caption.monospaced())
                }
            } header: {
                Text("改了 \(pr.files?.count ?? 0) 个文件")
            }

            // 4. 完整 diff（折叠）
            if let diff = pr.diff, !diff.isEmpty {
                Section {
                    DisclosureGroup("完整改动") {
                        ScrollView(.horizontal) {
                            Text(diff)
                                .font(.system(size: 11, design: .monospaced))
                                .textSelection(.enabled)
                                .fixedSize(horizontal: true, vertical: false)
                                .padding(.vertical, 4)
                        }
                    }
                    .font(.subheadline)
                }
            }

            // 5. 按钮
            Section {
                Button("问问他") { onAsk(pr.char, "讲讲 #\(pr.id) 这个 PR") }
                if pr.status == "open" {
                    VStack(alignment: .leading, spacing: 3) {
                        Button("合并") {
                            if pr.tests?.ok == true { confirmMerge = true }
                            else { forceText = forceMessage(pr) }
                        }
                        .disabled(busy || !(pr.blockers ?? []).isEmpty)
                        if pr.tests?.ok != true {
                            Text("测试没过，合并前看一眼")
                                .font(.caption2)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                if PRLook.unfinished(pr.status) {
                    Button("关闭", role: .destructive) { closeReason = ""; closing = true }
                        .disabled(busy)
                }
            }
        }
        .listStyle(.insetGrouped)
    }

    private func statusLine(_ pr: PRDetail) -> String {
        var s = PRLook.label(pr.status)
        if pr.status == "merged" {
            s += pr.effective == true ? "，已生效" : "，还没生效"
            if pr.forced == true { s += "（强行合并）" }
        }
        return s
    }

    private func testsHeadline(_ pr: PRDetail) -> String {
        guard let t = pr.tests else { return "没有测试结果" }
        if t.ok == true { return "✅ 测试通过" }
        return "❌ " + (t.summary?.components(separatedBy: "：").first ?? "测试没过")
    }

    private func forceMessage(_ pr: PRDetail) -> String {
        let s = pr.tests?.summary ?? "这个 PR 没有测试结果"
        return s + "\n\n确定要合的话点「强行合并」；合完重启起不来，看守会自动退回它。"
    }

    private func load() async {
        do {
            pr = try await service.getPR(id)
            unapplied = (try? await service.getPRs().unapplied) ?? unapplied
            errorText = nil
        } catch {
            errorText = error.localizedDescription
        }
    }

    private func merge(force: Bool) async {
        busy = true
        defer { busy = false }
        do {
            try await service.mergePR(id, force: force)
        } catch PRActionError.needsForce(let m) {
            forceText = m + "\n\n确定要合的话点「强行合并」。"
            return
        } catch {
            opError = error.localizedDescription
        }
        await load()
        await onChanged()
    }

    private func close() async {
        busy = true
        defer { busy = false }
        do { try await service.closePR(id, reason: closeReason) }
        catch { opError = error.localizedDescription }
        await load()
        await onChanged()
    }
}
