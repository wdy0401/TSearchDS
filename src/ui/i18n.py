# SPDX-License-Identifier: GPL-3.0-or-later
# See COPYING / LICENSE and NOTICE.md.
"""Small, offline English/Chinese interface catalog.

English is the default language and the source strings in the code are Chinese
(historical), so :data:`CATALOG` maps Chinese -> English and :func:`tr` looks up
the reverse direction when Chinese is selected.  Nothing here touches the
network: both directions are resolved from the same in-process table.
"""
import re

#: Language used when nothing has been chosen yet or an unknown code is given.
DEFAULT_LANGUAGE = 'en'
#: Supported interface languages.
LANGUAGES = ('en', 'zh_CN')

_language = DEFAULT_LANGUAGE
#: Chinese -> English.  Call :func:`tr` instead of reading this directly.
CATALOG = {
    '名称': 'Name', '资源数': 'Availability', '文件大小': 'File size',
    '文件类型': 'File type', '链接': 'Link', '来源': 'Source',
    '原始链接': 'Original link', '磁力 (magnet)': 'Magnet',
    '电驴 (ed2k)': 'eD2k', '迅雷 (thunder://)': 'Thunder',
    '视频': 'Video', '音频': 'Audio', '图片': 'Image', '文档': 'Document',
    '程序': 'Application', '压缩包': 'Archive', '光盘镜像': 'Disk image',
    '未知': 'Unknown', '其他': 'Other',
    '搜索（新标签）': 'Search (new tab)', '停止当前': 'Stop current',
    '复制选中链接': 'Copy selected links', '复制全部结果': 'Copy all results',
    '复制全部结果链接': 'Copy all result links', '复制全部': 'Copy all',
    '全选': 'Select all', '反选': 'Invert selection', '新建标签': 'New tab',
    '数据源': 'Sources', '导入代理内容': 'Import proxy configuration',
    '节点测速': 'Test nodes', '就绪': 'Ready', '代理: 未启动': 'Proxy: not started',
    '文件(&F)': '&File', '搜索(&S)': '&Search', '链接(&L)': '&Links',
    '视图(&V)': '&View', '数据源(&D)': 'S&ources', '代理(&P)': '&Proxy',
    '帮助(&H)': '&Help', '关于': 'About', '退出': 'Exit',
    '新建搜索标签': 'New search tab', '关闭当前标签': 'Close current tab',
    '打开数据目录': 'Open data directory', '查看 mihomo 日志': 'View mihomo log',
    '聚焦搜索框': 'Focus search input',
    '重置当前标签的列布局': 'Reset current tab columns',
    '按窗口宽度分配列宽': 'Fit columns to window',
    '全部启用': 'Enable all', '全部禁用': 'Disable all',
    '启动代理': 'Start proxy', '停止代理': 'Stop proxy',
    '节点测速并切换最快': 'Test nodes and select fastest',
    '重新获取订阅': 'Refresh subscription', '设置订阅地址…': 'Set subscription URL…',
    '导入代理内容（粘贴文本）…': 'Import proxy configuration (paste)…',
    '导入代理文件…': 'Import proxy file…', '清空导入的节点': 'Clear imported nodes',
    '选择': 'Select', '全部': 'All', '自动': 'Automatic', '(空)': '(Empty)',
    '等待代理就绪，随后自动搜索…': 'Waiting for proxy; search will start automatically…',
    '设置代理订阅': 'Proxy subscription',
    '填写 HTTP/HTTPS 订阅地址；仅保存在本机：': 'Enter an HTTP/HTTPS subscription URL (stored locally):',
    '订阅地址无效': 'Invalid subscription URL',
    '请填写有效的 HTTP/HTTPS 订阅地址。': 'Enter a valid HTTP/HTTPS subscription URL.',
    '订阅已保存，正在获取节点…': 'Subscription saved; fetching nodes…',
    '代理: 启动中…': 'Proxy: starting…', '代理: 已停止': 'Proxy: stopped',
    '节点测速中…': 'Testing nodes…', '正在重新获取订阅…': 'Refreshing subscription…',
    '已清除手动导入的节点，下次刷新订阅后生效': 'Imported nodes cleared; takes effect on the next refresh',
    '链接格式:': 'Link format:', '链接格式: %s': 'Link format: %s',
    '代理未配置，将以直连模式搜索': 'Proxy not configured; using direct search',
    '请输入关键词': 'Enter search keywords', '没有数据源': 'No sources enabled',
    '请在“数据源”菜单中启用至少一个数据源。': 'Enable at least one source in the Sources menu.',
    '没有可复制的链接': 'No links to copy',
    '已复制 %d 条%s链接到剪贴板': 'Copied %d %s links to clipboard',
    '选中结果没有磁力链接': 'Selected results have no magnet links',
    '代理: 未配置订阅': 'Proxy: no subscription',
    '可在“代理 → 设置订阅地址”配置；当前使用直连': 'Set a subscription in Proxy → Set subscription URL; using direct connection',
    '代理未运行，先启动代理…': 'Proxy not running; starting…',
    '正在校验并应用 %d 个导入节点…': 'Validating and applying %d imported nodes…',
    '代理: %s': 'Proxy: %s', '暂无 mihomo 日志': 'No mihomo log available',
    '代理: 订阅失败 → %s': 'Proxy: subscription failed → %s',
    '订阅获取失败': 'Subscription fetch failed', '代理: 就绪 → %s': 'Proxy: ready → %s',
    '订阅已更新 (%d 节点)': 'Subscription updated (%d nodes)',
    '代理: 切换中 → %s': 'Proxy: switching → %s',
    '代理: 运行中 → %s  ·  %dms  ·  %d个节点': 'Proxy: running → %s  ·  %dms  ·  %d nodes',
    '导入失败: %s': 'Import failed: %s', '导入节点': 'Imported nodes',
    '在本标签的结果里过滤…': 'Filter results in this tab…',
    '选中一行查看详情': 'Select a row to see details', '过滤:': 'Filter:',
    '搜索中… (%d 个数据源)': 'Searching… (%d sources)',
    '“%s” 搜索中… (%d 个数据源)': 'Searching “%s”… (%d sources)',
    '已停止': 'Stopped', '%d 条 · %.0fs': '%d results · %.0fs', '%d 条': '%d results',
    '数据源错误: %s': 'Source error: %s', '%s (%d) 数据源错误': '%s (%d) source errors',
    '名称: %s': 'Name: %s', '资源数: %s%s': 'Availability: %s%s',
    '文件大小: %s%s': 'File size: %s%s', '文件类型: %s': 'File type: %s',
    '来源: %s': 'Source: %s', '种子/来源数: %s': 'Seeds/availability: %s',
    '原始: %s': 'Original: %s', '页面: %s': 'Page: %s',
    '复制名称': 'Copy names', '重新获取资源数': 'Refresh availability',
    '在浏览器中打开页面': 'Open page in browser',
    '正在重新获取资源数 (%d)…': 'Refreshing availability (%d)…',
    '该结果没有可打开的页面': 'This result has no page to open',
    '该结果只有带关键词的搜索页，不打开（避免关键词进入浏览器历史）': 'Only a search page is available; it will not be opened to protect browser history',
    '列布局已重置为默认（名称/资源数/大小/类型/链接/来源）': 'Column layout reset (name/availability/size/type/link/source)',
    '结果': 'Results', '详情': 'Details', '下载中(leechers): %d': 'Leechers: %d',
    '  (未确认)': '  (unconfirmed)', '  (取自文件名，仅供参考)': '  (estimated from filename)',
    '（无法转换）': '(cannot convert)',
    '导入代理内容 · from ds': 'Import proxy configuration · from ds',
    '从剪贴板粘贴': 'Paste from clipboard', '从文件导入…': 'Import from file…',
    '取消': 'Cancel', '导入并使用': 'Import and use', '清空': 'Clear',
    '等待内容…': 'Waiting for input…', '读取失败': 'Read failed',
    '选择配置文件': 'Select configuration file', '没有可用节点': 'No usable nodes',
    '没有从内容中解析出任何可用节点。': 'No usable nodes could be parsed from the input.',
    '✔ 识别到 %d 个节点 (%s)': '✔ Found %d nodes (%s)',
    '支持 Clash / mihomo YAML 全文、proxies 片段、节点链接列表、base64 订阅。': 'Supports Clash/mihomo YAML, proxies fragments, node URL lists and base64 subscriptions.',
    '配置文件 (*.yaml *.yml *.txt *.conf *.json);;所有文件 (*)': 'Configuration files (*.yaml *.yml *.txt *.conf *.json);;All files (*)',
    '输入关键词（支持模糊匹配，例如：火影 1080p / ubuntu / naruto）': 'Enter keywords (fuzzy matching, e.g. ubuntu / naruto 1080p)',
    '每次搜索都会新开一个标签，可以在上一个还没出结果时接着搜下一个 (Ctrl+Enter)': 'Each search opens a new tab; searches can run concurrently (Ctrl+Enter)',
    '只停止当前标签的搜索；关闭标签也会停止它': 'Stop the current tab only; closing a tab also stops its search',
    '选择链接的显示格式（复制时也使用该格式，所有标签）': 'Choose the display and copy format for links in all tabs',
    '每行一个链接 (Ctrl+C)': 'One link per line (Ctrl+C)',
    '粘贴 Clash/mihomo 配置或节点链接，作为代理来源': 'Paste Clash/mihomo configuration or node URLs for the proxy',
    'TSearch-DS · 磁力 / 电驴 / 迅雷 搜索   from ds': 'TSearch-DS · Magnet / eD2k / Thunder Search   from ds',
}


def language():
    return _language


def set_language(value):
    """Select a language; an unknown code falls back to :data:`DEFAULT_LANGUAGE`."""
    global _language
    _language = value if value in LANGUAGES else DEFAULT_LANGUAGE


def _reverse():
    """English -> Chinese, derived lazily so later ``CATALOG.update`` is included."""
    global _REVERSE
    if _REVERSE is None:
        table = {}
        for chinese, english in CATALOG.items():
            table.setdefault(english, chinese)
        _REVERSE = table
    return _REVERSE


def tr(text):
    """Translate a source string written in Chinese into the active language.

    English is the default, so the common path is the forward lookup; Chinese
    simply falls back to the source text when there is nothing to restore.
    """
    if _language == 'zh_CN':
        return _reverse().get(text, text)
    return CATALOG.get(text, text)


def retranslate(text, old_language):
    """Translate existing interface text, including percent-formatted statuses."""
    if old_language == _language:
        return text
    for chinese, english in CATALOG.items():
        before, after = (chinese, english) if _language == 'en' else (english, chinese)
        if text == before:
            return after
        specs = list(re.finditer(r'%[.\d]*[sdf]', before))
        if not specs:
            continue
        pieces, last = [], 0
        for spec in specs:
            pieces.extend((re.escape(before[last:spec.start()]), '(.*?)'))
            last = spec.end()
        pieces.append(re.escape(before[last:]))
        match = re.fullmatch(''.join(pieces), text, re.DOTALL)
        if match:
            values = iter(match.groups())
            return re.sub(r'%[.\d]*[sdf]', lambda _: next(values), after)
    return text


def translate_status(text):
    if language() != 'en':
        return text
    output = retranslate(text, 'zh_CN')
    for original, translated in (('条结果', 'results'), ('个源有响应', 'sources responded'),
            ('资源数补充完成', 'Availability enrichment finished'), ('补充资源数', 'Enriching availability'),
            ('条 /', 'results /'), ('完成', 'Done'), ('错误', 'error')):
        output = output.replace(original, translated)
    return output


CATALOG.update({
    '表格中链接的显示格式；复制时也使用该格式（所有标签）': 'Display and copy format for links in all tabs',
    '导入配置文件…': 'Import configuration file…', '清空已导入的节点': 'Clear imported nodes',
    '等待代理就绪后自动搜索…': 'Waiting for proxy before starting search…', '选中': 'selected',
    '填写 HTTP/HTTPS 订阅地址（仅保存在本机）：': 'Enter an HTTP/HTTPS subscription URL (stored locally):',
    '已清空手动导入的节点，下次刷新订阅后生效': 'Imported nodes cleared; takes effect on the next refresh',
    '%s\n数据目录: %s': '%s\nData directory: %s',
    '代理未就绪，仍以直连方式搜索': 'Proxy not ready; using direct search',
    '请在「数据源」菜单中至少启用一个数据源。': 'Enable at least one source in the Sources menu.',
    '没有可复制的内容': 'Nothing to copy', '选中结果中没有磁力链接': 'Selected results have no magnet links',
    '请填写完整的 HTTP/HTTPS 订阅地址。': 'Enter a complete HTTP/HTTPS subscription URL.',
    '可在「代理 → 设置订阅地址」配置；当前使用直连': 'Set a subscription in Proxy → Set subscription URL; using direct connection',
    '代理未运行，先启动代理': 'Proxy not running; starting proxy',
    '代理: 已运行 · %s': 'Proxy: running · %s', '代理: 错误 · %s': 'Proxy: error · %s',
    '代理: 运行中 · %s · %dms · %d个可用': 'Proxy: running · %s · %dms · %d available',
    '代理: 运行中 · %s': 'Proxy: running · %s',
    '「%s」搜索中… (%d 个数据源)': 'Searching “%s”… (%d sources)',
    '补充资源数… %s': 'Enriching availability… %s',
    '%s (%d) 补资源数…': '%s (%d) enriching availability…', '完成': 'Done',
    '文件大小: %s': 'File size: %s',
    "<span style='color:#8b949e'>（大小取自文件名，仅供参考）</span>": "<span style='color:#8b949e'>(estimated from filename)</span>",
    '動漫花園 (dmhy)': 'DMHY', '蜜柑计划 (Mikan)': 'Mikan',
    # File-type categories produced by src/core/filetypes.py.  Two of them were
    # missing, so the English UI showed the Chinese label.
    '电驴收藏': 'eD2k collection', '其它': 'Other',
    'Internet Archive（公开种子）': 'Internet Archive (public torrents)',
    'eD2k 服务器 (ed2k hub)': 'eD2k servers (ed2k hub)', 'eD2k Kad (电驴网络)': 'eD2k Kad network',
    '%d 条结果 · %d/%d 个源有响应 · %.1fs': '%d results · %d/%d sources responded · %.1fs',
    '%d 条结果 · %d/%d 个源有响应 · %.1fs（%.0fs 上限已到，慢的源按无结果计）': '%d results · %d/%d sources responded · %.1fs (%.0fs time limit reached; slow sources counted as empty)',
})

CATALOG["<h3>TSearch-DS</h3><p>磁力 / eD2k(电驴) / 迅雷 链接聚合搜索器 &nbsp;<b>from ds</b></p><p>结果列 <b>名称 / 资源数 / 文件大小 / 文件类型 / 链接 / 来源</b>；表头可拖动换序、可调宽度；多选后按 Ctrl+C 复制（每行一个链接）。</p><p>每次搜索新开一个标签，可以并行搜索。</p><p>不建立搜索历史；查询会发送至所选站点，网络服务可能记录请求。</p><p>可选 mihomo 代理核心由用户自行从官方项目获取，使用用户提供的订阅、测速选优，节点失效时自动切换到可用节点。</p><p>GPL-3.0-or-later，许可证见 COPYING。软件按现状提供，无担保，以适用法律允许的范围为限。搜索结果不代表获得内容使用许可。</p><p style='color:#8b949e'>%s</p>"] = '<h3>TSearch-DS</h3><p>Magnet / eD2k / Thunder link search.</p><p>Six columns: name, availability, file size, file type, link and source. Drag or resize headers; Ctrl+C copies one link per line.</p><p>Searches run in independent tabs.</p><p>Queries are sent to external sites, which may log requests.</p><p>Optional mihomo core is obtained by the user from the official project. Subscriptions are supplied by the user.</p><p>GPL-3.0-or-later; see COPYING. Provided as is, without warranty to the extent allowed by law. Results do not grant content permissions.</p><p>%s</p>'

CATALOG['在此粘贴代理配置或节点内容，例如：\n\n  • FlClash / Clash Verge / mihomo 导出的完整配置（含 proxies: 段）\n  • 只有 proxies: 的 YAML 片段\n  • 一行一个的节点链接：vless:// vmess:// trojan:// ss:// hysteria2:// tuic://\n  • base64 编码的订阅内容\n\n导入后会自动剔除 mihomo 无法加载的节点，并与订阅合并使用。'] = 'Paste proxy configuration or node URLs here:\n\n• Full Clash/mihomo YAML with proxies\n• A proxies-only YAML fragment\n• Node URLs, one per line\n• A base64 subscription\n\nUnsupported nodes are removed before merging with subscriptions.'


# Command line and diagnostic texts (``main.py``); kept here so the whole
# catalog lives in one place.
CATALOG.update({
    '不启动内置 mihomo 代理': 'Do not start the bundled mihomo proxy',
    '启动后立即搜索的关键词': 'Keyword to search for right after start-up',
    '只做自检（依赖、mihomo、订阅），不进 GUI': 'Run self-test only (dependencies, mihomo, subscription); no GUI',
    '关键词': 'KEYWORD',
    '无界面跑一次完整流程（启动代理+搜索），结果写文件后退出':
        'Run the full flow headless (proxy + search), write a report and exit',
    '--smoke 的总时间预算': 'Total time budget for --smoke',
    'TSearch-DS 出错': 'TSearch-DS error',
    'TSearch-DS 启动失败，详情见 crash.txt': 'TSearch-DS failed to start; see crash.txt',
    '子进程可随主进程一并终止': 'child processes die with the parent',
    '强杀程序时 mihomo 可能残留，下次启动会自动清理':
        'if the app is killed, mihomo may linger; it is cleaned up on the next start',
    '文件类型分布 : %s': 'File type breakdown: %s',
    '有文件大小   : %d/%d': 'With file size: %d/%d',
})


# Plain-text fragments used by the Tk backend, which has no HTML renderer and
# therefore splits the "About" box into separate strings.
CATALOG.update({
    '磁力 / eD2k(电驴) / 迅雷 链接聚合搜索器  from ds':
        'Magnet / eD2k / Thunder link search  from ds',
    '结果列 名称 / 资源数 / 文件大小 / 文件类型 / 链接 / 来源；'
    '表头可拖动换序、可调宽度；多选后按 Ctrl+C 复制（每行一个链接）。':
        'Columns: name, availability, file size, file type, link and source. '
        'Headers can be reordered and resized; Ctrl+C copies one link per line.',
    '每次搜索新开一个标签，可以并行搜索。':
        'Every search opens its own tab, so searches run in parallel.',
    '不建立搜索历史；查询会发送至所选站点，网络服务可能记录请求。':
        'No search history is kept; queries are sent to external sites, which '
        'may log requests.',
    '可选 mihomo 代理核心由用户自行从官方项目获取，使用用户提供的订阅、测速选优，'
    '节点失效时自动切换到可用节点。':
        'The optional mihomo core is obtained by the user from the official '
        'project; it uses the subscription you supply and switches to the '
        'fastest working node.',
    'GPL-3.0-or-later，许可证见 COPYING。软件按现状提供，无担保，'
    '以适用法律允许的范围为限。搜索结果不代表获得内容使用许可。':
        'GPL-3.0-or-later; see COPYING. Provided as is, without warranty to '
        'the extent allowed by law. Results do not grant content permissions.',
    '确定': 'OK',
    '（大小取自文件名，仅供参考）': '(estimated from filename)',
    '左移「%s」': 'Move “%s” left',
    '右移「%s」': 'Move “%s” right',
})


#: Cache for the derived English -> Chinese table (see :func:`_reverse`).
_REVERSE = None
