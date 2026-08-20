import SwiftUI

// MARK: - 数据模型（对齐后端 /jobhunt/jds、/jobhunt/applications）

/// 岗位库条目（列表版：text 只有开头）。库是全局一份，谁经手记在 char_id 上。
struct JobhuntJd: Decodable, Identifiable {
    let id: String
    let ts: String?
    let source: String?
    let company: String?
    let title: String?
    let score: Double?
    let status: String?           // new / scored / drafted / sent / archived
    let char_id: String?
    let text_head: String?

    var displayName: String {
        let c = (company ?? "").trimmingCharacters(in: .whitespaces)
        let t = (title ?? "").trimmingCharacters(in: .whitespaces)
        if c.isEmpty && t.isEmpty { return "（没写公司/岗位）" }
        return [c, t].filter { !$0.isEmpty }.joined(separator: " · ")
    }
}

struct JobhuntJdsWrap: Decodable { let items: [JobhuntJd] }

/// 岗位详情（全文 + 打分理由）。
struct JobhuntJdFull: Decodable {
    let id: String
    let ts: String?
    let source: String?
    let company: String?
    let title: String?
    let text: String?
    let score: Double?
    let score_reason: String?
    let status: String?
    let char_id: String?
}

/// 投递台账一条（真发出才有；has_reply 由 watcher 的回信匹配标）。
struct JobhuntApplication: Decodable, Identifiable {
    let id: String
    let jd_id: String?
    let company: String?
    let title: String?
    let to: String?
    let resume_id: String?
    let subject: String?
    let sent_at: String?
    let status: String?
    let has_reply: Bool?
    let char_id: String?
    let reply_at: String?
    let reply_from: String?
    let reply_subject: String?
    let reply_snippet: String?
}

struct JobhuntAppsWrap: Decodable { let items: [JobhuntApplication] }

extension ChatService {
    func getJobhuntJds() async throws -> [JobhuntJd] {
        let data = try await perform(authedRequest("GET", "/jobhunt/jds?limit=300"))
        do { return try JSONDecoder().decode(JobhuntJdsWrap.self, from: data).items }
        catch { throw ChatServiceError.badResponse }
    }

    func getJobhuntJd(id: String) async throws -> JobhuntJdFull {
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        let data = try await perform(authedRequest("GET", "/jobhunt/jds/\(encoded)"))
        do { return try JSONDecoder().decode(JobhuntJdFull.self, from: data) }
        catch { throw ChatServiceError.badResponse }
    }

    func getJobhuntApplications() async throws -> [JobhuntApplication] {
        let data = try await perform(authedRequest("GET", "/jobhunt/applications?limit=100"))
        do { return try JSONDecoder().decode(JobhuntAppsWrap.self, from: data).items }
        catch { throw ChatServiceError.badResponse }
    }

    func setJobhuntArchived(id: String, archived: Bool) async throws {
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        _ = try await perform(authedRequest("POST",
            "/jobhunt/jds/\(encoded)/\(archived ? "archive" : "unarchive")"))
    }
}

// MARK: - JD 库页（抽屉 → push；全局一份，不分角色）

/// 求职流水线的机主视角：岗位按状态走到哪了、打了多少分、投出去的有没有回音。
/// 「不投」（archive）是机主的否决权——TA 打了分的岗，不想投的在这里划掉。
struct JobhuntPage: View {
    private let service = ChatService()

    @State private var jds: [JobhuntJd] = []
    @State private var apps: [JobhuntApplication] = []
    @State private var loading = true
    @State private var errorText: String? = nil

    /// 状态 → 展示顺序和标题。new 在最上（等着筛的），sent 和 archived 沉底。
    private static let sections: [(status: String, title: String)] = [
        ("new", "待筛（还没打分）"),
        ("scored", "已打分"),
        ("drafted", "已起草（草稿信箱里等确认）"),
        ("sent", "已投出"),
        ("archived", "不投"),
    ]

    var body: some View {
        Group {
            if loading {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorText {
                ContentUnavailableView("读不到岗位库", systemImage: "exclamationmark.triangle",
                                       description: Text(errorText))
            } else if jds.isEmpty {
                ContentUnavailableView("岗位库还空着", systemImage: "briefcase",
                                       description: Text("TA 存进来的岗位、订阅邮件里筛出的岗位都会出现在这里。"))
            } else {
                List {
                    ForEach(Self.sections, id: \.status) { sec in
                        let rows = grouped(sec.status)
                        if !rows.isEmpty {
                            Section(sec.title) {
                                ForEach(rows) { jd in
                                    NavigationLink {
                                        JobhuntJdDetail(jdID: jd.id,
                                                        applications: apps.filter { $0.jd_id == jd.id },
                                                        onChanged: { Task { await load() } })
                                    } label: { row(jd) }
                                }
                            }
                        }
                    }
                }
                .listStyle(.insetGrouped)
            }
        }
        .background(Color(.systemGroupedBackground))
        .navigationTitle("求职 · JD 库")
        .navigationBarTitleDisplayMode(.inline)
        .refreshable { await load() }
        .task { await load() }
    }

    /// 组内排序：已打分的分高在前，其余按时间新→旧（列表本来就是新→旧）。
    private func grouped(_ status: String) -> [JobhuntJd] {
        let rows = jds.filter { ($0.status ?? "new") == status }
        if status == "scored" {
            return rows.sorted { ($0.score ?? -1) > ($1.score ?? -1) }
        }
        return rows
    }

    @ViewBuilder
    private func row(_ jd: JobhuntJd) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(jd.displayName)
                    .font(.subheadline.bold()).foregroundStyle(.primary).lineLimit(1)
                Spacer()
                if let score = jd.score {
                    Text(String(format: "%.0f", score))
                        .font(.subheadline.monospacedDigit().bold())
                        .foregroundStyle(score >= 70 ? Color.theme : .secondary)
                }
            }
            if let head = jd.text_head, !head.isEmpty {
                Text(head).font(.footnote).foregroundStyle(.secondary).lineLimit(2)
            }
            HStack(spacing: 8) {
                if let src = jd.source, !src.isEmpty {
                    Text(src).lineLimit(1)
                }
                if let ts = jd.ts { Text(ts) }
                Spacer()
                // 回信角标：这条对应的投递有 HR 回音
                if apps.contains(where: { $0.jd_id == jd.id && ($0.has_reply ?? false) }) {
                    Label("有回信", systemImage: "arrowshape.turn.up.left.fill")
                        .foregroundStyle(Color.theme)
                }
            }
            .font(.caption)
            .foregroundStyle(.tertiary)
        }
    }

    private func load() async {
        errorText = nil
        do {
            jds = try await service.getJobhuntJds()
            apps = try await service.getJobhuntApplications()
        } catch {
            errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端"
        }
        loading = false
    }
}

// MARK: - 岗位详情

struct JobhuntJdDetail: View {
    let jdID: String
    let applications: [JobhuntApplication]
    var onChanged: (() -> Void)? = nil

    private let service = ChatService()

    @State private var jd: JobhuntJdFull? = nil
    @State private var loading = true
    @State private var errorText: String? = nil
    @State private var busy = false
    @State private var opError: String? = nil
    @State private var pdfBusy = false
    @State private var pdfPreview: PdfPreviewItem? = nil

    var body: some View {
        Group {
            if loading {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let errorText {
                ContentUnavailableView("读不到这个岗位", systemImage: "exclamationmark.triangle",
                                       description: Text(errorText))
            } else if let jd {
                List {
                    Section("岗位") {
                        if let c = jd.company, !c.isEmpty { LabeledContent("公司") { Text(c) } }
                        if let t = jd.title, !t.isEmpty { LabeledContent("岗位") { Text(t) } }
                        if let s = jd.source, !s.isEmpty {
                            LabeledContent("来源") { Text(s).lineLimit(2) }
                        }
                        if let ts = jd.ts { LabeledContent("入库") { Text(ts) } }
                        LabeledContent("状态") { Text(statusText(jd.status ?? "new")) }
                    }
                    if let score = jd.score {
                        Section("打分") {
                            LabeledContent("匹配分") {
                                Text(String(format: "%.0f", score)).bold()
                            }
                            if let r = jd.score_reason, !r.isEmpty {
                                Text(r).font(.subheadline).foregroundStyle(.secondary)
                            }
                            if let by = jd.char_id, !by.isEmpty {
                                LabeledContent("经手") { Text(by) }
                            }
                        }
                    }
                    if !applications.isEmpty {
                        Section("投递记录") {
                            ForEach(applications) { app in applicationRow(app) }
                        }
                    }
                    Section("JD 全文") {
                        Text(jd.text ?? "（空）")
                            .font(.footnote)
                            .textSelection(.enabled)
                    }
                    // 机主的否决权：不想投的划掉（TA 的 jd_list 里 archived 不占收件箱）；
                    // 已投出的没有这个开关——信都发了，划掉只会骗自己。
                    if jd.status != "sent" {
                        Section {
                            Button {
                                Task { await toggleArchive() }
                            } label: {
                                HStack {
                                    Spacer()
                                    if busy { ProgressView() }
                                    else {
                                        Text(jd.status == "archived" ? "恢复（重新考虑这个岗）" : "标「不投」")
                                            .foregroundStyle(jd.status == "archived" ? Color.theme : .red)
                                    }
                                    Spacer()
                                }
                            }
                            .disabled(busy)
                        }
                    }
                }
                .listStyle(.insetGrouped)
            }
        }
        .navigationTitle(navTitle)
        .navigationBarTitleDisplayMode(.inline)
        .sheet(item: $pdfPreview) { item in QuickLookPreview(url: item.url) }
        .alert("操作失败", isPresented: Binding(
            get: { opError != nil }, set: { if !$0 { opError = nil } }
        )) { Button("好", role: .cancel) { } } message: { Text(opError ?? "") }
        .task { await load() }
    }

    @ViewBuilder
    private func applicationRow(_ app: JobhuntApplication) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(app.to ?? "").font(.footnote.bold())
                Spacer()
                Text(app.sent_at ?? "").font(.caption).foregroundStyle(.secondary)
            }
            if let rid = app.resume_id, !rid.isEmpty {
                Button {
                    Task { await openPdf(rid) }
                } label: {
                    HStack(spacing: 4) {
                        if pdfBusy { ProgressView().controlSize(.small) }
                        Image(systemName: "doc.richtext")
                        Text("简历：\(rid)").lineLimit(1)
                    }
                    .font(.caption)
                }
                .disabled(pdfBusy)
            }
            if app.has_reply ?? false {
                VStack(alignment: .leading, spacing: 2) {
                    Label("HR 回信 \(app.reply_at ?? "")", systemImage: "arrowshape.turn.up.left.fill")
                        .font(.caption.bold()).foregroundStyle(Color.theme)
                    if let snip = app.reply_snippet, !snip.isEmpty {
                        Text(snip).font(.caption).foregroundStyle(.secondary).lineLimit(3)
                    }
                }
            }
            if let by = app.char_id, !by.isEmpty {
                Text("经手：\(by)").font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .padding(.vertical, 2)
    }

    private var navTitle: String {
        guard let jd else { return "岗位" }
        let parts = [jd.company ?? "", jd.title ?? ""].filter { !$0.isEmpty }
        return parts.isEmpty ? "岗位" : parts.joined(separator: "·")
    }

    private func statusText(_ s: String) -> String {
        switch s {
        case "new": return "待筛"
        case "scored": return "已打分"
        case "drafted": return "已起草，等你在草稿信箱确认"
        case "sent": return "已投出"
        case "archived": return "不投"
        default: return s
        }
    }

    private func load() async {
        errorText = nil
        do { jd = try await service.getJobhuntJd(id: jdID) }
        catch { errorText = (error as? ChatServiceError)?.errorDescription ?? "连不上后端" }
        loading = false
    }

    private func toggleArchive() async {
        guard let cur = jd else { return }
        busy = true
        do {
            try await service.setJobhuntArchived(id: cur.id, archived: cur.status != "archived")
            await load()
            onChanged?()
        } catch {
            opError = (error as? ChatServiceError)?.errorDescription ?? "改不动"
        }
        busy = false
    }

    private func openPdf(_ resumeId: String) async {
        pdfBusy = true
        do {
            let url = try await service.downloadJobhuntPdf(resumeId: resumeId, fileName: "")
            pdfPreview = PdfPreviewItem(url: url)
        } catch {
            opError = (error as? ChatServiceError)?.errorDescription ?? "简历还没渲染成 PDF"
        }
        pdfBusy = false
    }
}
