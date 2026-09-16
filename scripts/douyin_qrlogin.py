#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
douyin_qrlogin.py — 抖音网页版「扫码+刷脸登录」独立脚本（无头模式）

流程:
  第1步: 无头启动 Chromium, 抓取登录二维码 douyin_qrcode.png;
  第2步: 手机抖音扫码确认后, 脚本自动点击网页上的「刷脸」选项;
  第3步: 页面出现刷脸二维码, 脚本抓取保存为 douyin_face_qrcode.png;
  第4步: 手机再扫刷脸二维码, 完成刷脸验证;
  第5步: 网页自动登录, 登录态持久化到 douyin_profile/ 目录;
         重启后自动恢复, 无需重新登录。

用法:
  python3 douyin_qrlogin.py                # 出二维码并等待扫码
  python3 douyin_qrlogin.py --timeout 600  # 自定义等待秒数
  python3 douyin_qrlogin.py --check        # 只检查当前登录态是否有效
"""

import os
import sys
import json
import time
import argparse

# ============ 配置区 ============
# 目录结构(脚本在 scripts/, 登录态在 conf/, 二维码/截图在 images/):
BASE_DIR    = os.path.dirname(os.path.abspath(__file__))    # scripts/ 目录
ROOT_DIR    = os.path.dirname(BASE_DIR)                     # 项目根 NapCatQQ-dokcer/
CONF_DIR    = os.path.join(ROOT_DIR, "conf")
IMAGE_DIR   = os.path.join(ROOT_DIR, "images")
PROFILE_DIR = os.path.join(CONF_DIR, "douyin_profile")      # 持久化浏览器配置
STATE_FILE  = os.path.join(CONF_DIR, "douyin_state.json")   # storage_state 备份
COOKIE_FILE = os.path.join(CONF_DIR, "douyin_cookies.json")
QR_FILE     = os.path.join(IMAGE_DIR, "douyin_qrcode.png")       # 登录二维码
FACE_QR_FILE = os.path.join(IMAGE_DIR, "douyin_face_qrcode.png")  # 刷脸二维码
DEBUG_PNG   = os.path.join(IMAGE_DIR, "douyin_login_debug.png")
HOME_URL    = "https://www.douyin.com/"
HEADLESS    = True
WAIT_SEC    = 600

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

LOGIN_COOKIE_NAMES = {"sessionid", "sessionid_ss", "sid_tt", "sid_guard"}

QR_SELECTORS = [
    # 已知容器
    "#douyin_login_comp_scan_code img[src^='data:image']",
    "#douyin_login_comp_scan_code img",
    "#douyin_login_comp_scan_code canvas",
    "#animate_qrcode_container img[src^='data:image']",
    ".web-login-scan-code__content img",
    ".web-login-scan-code__content canvas",
    # 登录弹窗内的 img/canvas(抖音前端改版后容器类名会变)
    "[class*='login'] [class*='scan'] img[src^='data:image']",
    "[class*='login'] [class*='scan'] canvas",
    "[class*='login'] [class*='qr'] img[src^='data:image']",
    "[class*='login'] [class*='qr'] canvas",
    # 通用兜底: 任意 data:image 的 img / canvas
    "img[src*='qrcode']",
    "canvas[class*='qr']",
    "[class*='qrcode'] img",
    "[class*='qrcode'] canvas",
    "[class*='scan-code'] img",
    "#login-panel-qrcode img",
    "#login-panel-qrcode canvas",
    # 最终兜底: 弹窗内所有 img 和 canvas(靠尺寸过滤)
    "[class*='modal'] img[src^='data:image']",
    "[class*='dialog'] img[src^='data:image']",
    "[class*='modal'] canvas",
    "[class*='dialog'] canvas",
]
REFRESH_SELECTORS = [
    ".web-login-refresh-icon", "[class*='refresh']",
    "text=点击刷新", "text=刷新二维码",
]

# 扫码后的验证页「刷脸」按钮选择器
FACE_VERIFY_SELECTORS = [
    "text=刷脸",
    "text=刷脸登录",
    "text=刷脸验证",
    "text=人脸验证",
    "button:has-text('刷脸')",
    "button:has-text('人脸')",
    "[class*='face']",
    "[class*='biometric']",
    "div:has-text('刷脸')",
    "span:has-text('刷脸')",
]
# 扫码后验证页关键词(检测页面是否从二维码切换到了验证页)
VERIFY_PAGE_KEYWORDS = ["刷脸", "人脸", "验证", "验证码", "手机号", "安全验证"]
# =========================================


def _log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def make_context(p):
    """持久化浏览器上下文 — cookies/localStorage 自动保存到 PROFILE_DIR。
    重启后自动恢复, 无需重新登录。"""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    context = p.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=HEADLESS,
        user_agent=USER_AGENT,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        viewport={"width": 1366, "height": 900},
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--lang=zh-CN",
            "--memory-pressure-off",
            "--disable-extensions",
            "--disable-plugins",
            "--disable-component-extensions-with-background-pages",
            "--disable-background-networking",
            "--js-flags=--max-old-space-size=128",
        ],
    )
    context.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    return context


def setup_page(page):
    def _route(route):
        if route.request.resource_type in ("font", "media"):
            return route.abort()
        return route.continue_()
    page.route("**/*", _route)


def goto_home(page, timeout=45000):
    page.goto(HOME_URL, wait_until="commit", timeout=timeout)


def is_logged_in(context):
    try:
        names = {c.get("name") for c in context.cookies()}
        return bool(names & LOGIN_COOKIE_NAMES)
    except Exception:
        return False


def _save_screenshot_unique(el, base_path):
    """截图保存到唯一文件名(防止图片查看器缓存), 同时原子更新 base_path。
    返回最终路径。"""
    import tempfile
    base_dir = os.path.dirname(base_path)
    base_name = os.path.basename(base_path)           # douyin_qrcode.png
    stem, ext = os.path.splitext(base_name)            # douyin_qrcode, .png
    # 找一个不冲突的序号
    n = getattr(_save_screenshot_unique, "_counter", 0) + 1
    _save_screenshot_unique._counter = n
    numbered = os.path.join(base_dir, f"{stem}_{n:03d}{ext}")
    # 先截到临时文件, 再原子替换(触发文件系统变更通知)
    tmp = numbered + ".tmp"
    el.screenshot(path=tmp)
    os.replace(tmp, numbered)
    # 同时原子更新标准文件名
    tmp2 = base_path + ".tmp"
    el.screenshot(path=tmp2)
    os.replace(tmp2, base_path)
    return numbered


def capture_qr(page):
    # 先用 CSS 选择器
    for sel in QR_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 4)):
                el = loc.nth(i)
                if el.is_visible():
                    box = el.bounding_box()
                    if box and box["width"] >= 80 and box["height"] >= 80:
                        numbered = _save_screenshot_unique(el, QR_FILE)
                        _log(f"📷 登录二维码 -> {os.path.basename(numbered)} ({sel})")
                        return True
        except Exception:
            continue
    # JS 终极兜底: 遍历所有元素(含 div background-image), 找二维码区域
    try:
        rect = page.evaluate("""() => {
            // 1. 先找「扫码登录」文字位置
            let scanTab = null;
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if (el.children.length === 0 && el.textContent.trim() === '扫码登录') {
                    scanTab = el;
                    break;
                }
            }
            // 2. 在扫码 tab 附近找方形元素(img/canvas/div with bg image)
            const checkEl = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 120 || r.width > 400 || r.height < 120 || r.height > 400)
                    return null;
                if (Math.abs(r.width - r.height) > 20) return null;
                const cs = window.getComputedStyle(el);
                const bg = cs.backgroundImage || '';
                const src = el.src || el.dataset.src || '';
                if (bg.includes('data:image') || bg.includes('blob:') ||
                    (src && src.startsWith('data:image')) ||
                    (src && src.startsWith('blob:')) ||
                    el.tagName === 'CANVAS') {
                    return {x: r.x, y: r.y, width: r.width, height: r.height};
                }
                return null;
            };
            // 从扫码 tab 往上找容器, 再在容器内找二维码
            if (scanTab) {
                let container = scanTab;
                for (let i = 0; i < 6; i++) {
                    container = container.parentElement;
                    if (!container) break;
                    const found = container.querySelectorAll('img, canvas, div, span');
                    for (const el of found) {
                        const r = checkEl(el);
                        if (r) return r;
                    }
                }
            }
            // 3. 全局搜索
            for (const el of all) {
                const r = checkEl(el);
                if (r) return r;
            }
            return null;
        }""")
        if rect:
            n = getattr(_save_screenshot_unique, "_counter", 0) + 1
            _save_screenshot_unique._counter = n
            stem = os.path.splitext(QR_FILE)[0]
            numbered = f"{stem}_{n:03d}.png"
            page.screenshot(path=numbered, clip=rect)
            # 原子更新标准文件名
            page.screenshot(path=QR_FILE + ".new.png", clip=rect)
            os.replace(QR_FILE + ".new.png", QR_FILE)
            _log(f"📷 登录二维码(JS区域截图) -> {os.path.basename(numbered)}")
            return True
    except Exception as e:
        _log(f"⚠️ JS 兜底失败: {e}")
    return False


def wait_for_qr(page, timeout_sec=30):
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if capture_qr(page):
            return True
        page.wait_for_timeout(1500)
    return False


def refresh_qr_on_page(page):
    """真正在网页上刷新二维码: 点击二维码区域/刷新按钮, 等新码加载, 再截图。"""
    # 1. 先点刷新按钮/文字(如果有)
    click_text_if_exists(page, REFRESH_SELECTORS, timeout=1500)
    # 2. 直接点击二维码区域(Douyin 过期后点击二维码本身会刷新)
    try:
        rect = page.evaluate("""() => {
            for (const el of document.querySelectorAll('img, canvas, div')) {
                const r = el.getBoundingClientRect();
                if (r.width >= 120 && r.width <= 400 && r.height >= 120 && r.height <= 400
                    && Math.abs(r.width - r.height) < 20) {
                    return {x: r.x + r.width/2, y: r.y + r.height/2};
                }
            }
            return null;
        }""")
        if rect:
            page.mouse.click(rect["x"], rect["y"])
    except Exception:
        pass
    # 3. 等新二维码加载
    page.wait_for_timeout(3500)
    # 4. 重新截图
    return capture_qr(page)


# 刷脸二维码的候选选择器(点完刷脸后页面上出现的新二维码)
FACE_QR_SELECTORS = [
    "#uc-second-verify img[src^='data:image']",
    "#uc-second-verify canvas",
    "[id*='face'] img[src^='data:image']",
    "[id*='face'] canvas",
    "[class*='face'] img[src^='data:image']",
    "[class*='face'] canvas",
    "[class*='qrcode'] img[src^='data:image']",
    "[class*='qrcode'] canvas",
    "[class*='qr-code'] img[src^='data:image']",
    "img[src^='data:image']",
    "canvas",
]


def capture_face_qr(page):
    """点完刷脸后抓取刷脸二维码, 保存到 FACE_QR_FILE(唯一文件名防缓存)。
    先用选择器, 再用 JS 区域截图兜底(同 capture_qr)。"""
    # CSS 选择器
    for sel in FACE_QR_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    box = el.bounding_box()
                    if box and box["width"] >= 80 and box["height"] >= 80:
                        numbered = _save_screenshot_unique(el, FACE_QR_FILE)
                        _log(f"📷 刷脸二维码 -> {os.path.basename(numbered)}")
                        return True
        except Exception:
            continue
    # JS 区域截图兜底: 在验证弹窗内找方形二维码
    try:
        rect = page.evaluate("""() => {
            const checkEl = (el) => {
                const r = el.getBoundingClientRect();
                if (r.width < 100 || r.width > 400 || r.height < 100 || r.height > 400)
                    return null;
                if (Math.abs(r.width - r.height) > 20) return null;
                const cs = window.getComputedStyle(el);
                const bg = cs.backgroundImage || '';
                const src = el.src || el.dataset.src || '';
                if (bg.includes('data:image') || bg.includes('blob:') ||
                    (src && (src.startsWith('data:image') || src.startsWith('blob:'))) ||
                    el.tagName === 'CANVAS') {
                    return {x: r.x, y: r.y, width: r.width, height: r.height};
                }
                return null;
            };
            // 优先在验证容器内找
            const containers = document.querySelectorAll(
                '#uc-second-verify, [class*="second-verify"], [class*="face"], [class*="verify"], [class*="modal"], [class*="dialog"]');
            for (const c of containers) {
                for (const el of c.querySelectorAll('img, canvas, div, span')) {
                    const r = checkEl(el);
                    if (r) return r;
                }
            }
            // 全局找
            for (const el of document.querySelectorAll('img, canvas, div')) {
                const r = checkEl(el);
                if (r) return r;
            }
            return null;
        }""")
        if rect:
            n = getattr(_save_screenshot_unique, "_counter", 0) + 1
            _save_screenshot_unique._counter = n
            stem = os.path.splitext(FACE_QR_FILE)[0]
            numbered = f"{stem}_{n:03d}.png"
            page.screenshot(path=numbered, clip=rect)
            page.screenshot(path=FACE_QR_FILE + ".new.png", clip=rect)
            os.replace(FACE_QR_FILE + ".new.png", FACE_QR_FILE)
            _log(f"📷 刷脸二维码(JS区域截图) -> {os.path.basename(numbered)}")
            return True
    except Exception as e:
        _log(f"⚠️ 刷脸二维码 JS 兜底失败: {e}")
    return False


def wait_for_face_qr(page, timeout_sec=30):
    """等待刷脸二维码出现(点击刷脸后可能有几秒延迟)。"""
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if capture_face_qr(page):
            return True
        page.wait_for_timeout(1500)
    return False


def click_text_if_exists(page, patterns, timeout=1500):
    for ptn in patterns:
        try:
            page.locator(ptn).first.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def save_state(context):
    try:
        context.storage_state(path=STATE_FILE)
    except Exception:
        pass
    cookies = context.cookies()
    simple = [{"name": c.get("name"), "value": c.get("value"),
               "domain": c.get("domain"), "path": c.get("path", "/")}
              for c in cookies]
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        json.dump(simple, f, ensure_ascii=False, indent=2)
    _log(f"💾 登录态已保存 ({len(cookies)} 个 cookie, 持久化目录: {PROFILE_DIR})")


def _find_visible(page, selectors):
    """从候选选择器列表中找到第一个可见元素。"""
    for sel in selectors:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    return el
        except Exception:
            continue
    return None


def is_qr_visible(page):
    """检查二维码元素是否还可见。"""
    for sel in QR_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 3)):
                if loc.nth(i).is_visible():
                    return True
        except Exception:
            continue
    return False


def detect_verify_page(page):
    """检测页面是否出现了刷脸验证选项(不管二维码是否还在)。
    直接搜索「刷脸」文字, 找到就返回 True。"""
    try:
        body = page.inner_text("body", timeout=2000)
        # 只要有「刷脸」就认为验证页出现了
        return "刷脸" in body
    except Exception:
        return False


def click_face_verify(page):
    """在验证页上找「刷脸」按钮并点击。
    策略: text=刷脸 点不到真正按钮, 改用 JS 遍历可点击元素找含「刷脸」的。"""
    # 方法1: 用 JS 找所有 button/a/div[role=button] 中含「刷脸」的, 逐个点
    try:
        clicked = page.evaluate("""() => {
            const sels = 'button, a, div[role="button"], div[onclick], [class*="verify"], [class*="face"], [class*="option"], [class*="item"], [class*="tab"]';
            const els = document.querySelectorAll(sels);
            for (const el of els) {
                const t = el.textContent || '';
                if (t.includes('刷脸') && t.length < 50) {
                    el.click();
                    return el.outerHTML.substring(0, 200);
                }
            }
            // 兜底: 找含「刷脸」的元素, 往上找 3 层父节点
            const all = document.querySelectorAll('*');
            for (const el of all) {
                if (el.children.length === 0 && el.textContent.includes('刷脸')) {
                    let p = el;
                    for (let i = 0; i < 3; i++) {
                        p = p.parentElement;
                        if (!p) break;
                        p.click();
                        return p.outerHTML.substring(0, 200);
                    }
                }
            }
            return null;
        }""")
        if clicked:
            _log(f"✅ 已点击「刷脸」(JS, 元素: {clicked[:100]})")
            return True
    except Exception as e:
        _log(f"⚠️ JS 点击失败: {e}")

    # 方法2: force click 原始选择器
    for sel in FACE_VERIFY_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 5)):
                el = loc.nth(i)
                if el.is_visible():
                    try:
                        el.click(timeout=3000, force=True)
                        _log(f"✅ 已点击「刷脸」(force, 选择器: {sel})")
                        return True
                    except Exception:
                        pass
        except Exception:
            continue
    return False


def fetch_self_info(page):
    resp_info = {}

    def _on_response(r):
        if "/aweme/v1/web/user/profile/" in r.url:
            try:
                j = r.json()
                if isinstance(j, dict) and j.get("user"):
                    resp_info.update(j)
            except Exception:
                pass
    page.on("response", _on_response)
    try:
        page.goto("https://www.douyin.com/user/self",
                  wait_until="commit", timeout=30000)
        for _ in range(12):
            page.wait_for_timeout(1000)
            if resp_info.get("user"):
                break
        user = resp_info.get("user") or {}
        dy_id = user.get("unique_id") or user.get("short_id")
        nick = user.get("nickname")
        return (str(dy_id) if dy_id else None), nick
    except Exception:
        return None, None


def do_login(wait_sec):
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = make_context(p)
        page = context.new_page()
        setup_page(page)
        try:
            _log(f"打开抖音首页(无头): {HOME_URL}")
            goto_home(page)
            page.wait_for_timeout(3000)

            # 已登录就直接退出
            if is_logged_in(context):
                _log("✅ 登录态有效(持久化配置), 无需重新登录")
                save_state(context)
                dy_id, nick = fetch_self_info(page)
                if dy_id:
                    _log(f"   抖音号: {dy_id}  昵称: {nick}")
                return 0

            # 出二维码
            if not wait_for_qr(page, timeout_sec=25):
                _log("未直接出现二维码, 尝试点击登录入口...")
                click_text_if_exists(page, ["text=登录"], timeout=3000)
                page.wait_for_timeout(2000)
                click_text_if_exists(page, ["text=扫码登录", "text=二维码登录"],
                                     timeout=2000)
                page.wait_for_timeout(1500)
                if not wait_for_qr(page, timeout_sec=30):
                    page.screenshot(path=DEBUG_PNG)
                    _log(f"❌ 没找到二维码, 整页截图: {DEBUG_PNG}")
                    return 2

            _log(f"⏳ 第 1 步: 请用手机抖音 App 扫描登录二维码 {QR_FILE}")
            _log("   扫码确认后脚本会自动点击「刷脸」")
            deadline = time.time() + wait_sec
            last_qr_capture = time.time()
            face_clicked = False
            face_qr_captured = False
            face_qr_last_capture = 0
            while time.time() < deadline:
                page.wait_for_timeout(3000)

                # 检测登录成功
                if is_logged_in(context):
                    page.wait_for_timeout(3000)
                    save_state(context)
                    dy_id, nick = fetch_self_info(page)
                    if dy_id:
                        _log(f"✅ 登录成功! 抖音号: {dy_id}  昵称: {nick}")
                    else:
                        _log("✅ 登录成功!")
                    return 0

                # 阶段1: 检测验证页 → 点刷脸
                if not face_clicked and detect_verify_page(page):
                    page.screenshot(path=DEBUG_PNG)
                    _log("📱 检测到验证页, 自动点击「刷脸」...")
                    if click_face_verify(page):
                        face_clicked = True
                        _log("✅ 已点击刷脸, 等待刷脸页面加载...")
                        # 可能还需要点「下一步/确认/开始验证/开始刷脸」
                        page.wait_for_timeout(2000)
                        for txt in ["下一步", "确认", "开始验证", "开始刷脸", "立即刷脸", "继续"]:
                            try:
                                btn = page.locator(f"button:has-text('{txt}')").first
                                if btn.is_visible():
                                    btn.click(timeout=2000, force=True)
                                    _log(f"✅ 点击了「{txt}」按钮")
                                    page.wait_for_timeout(2000)
                                    break
                            except Exception:
                                continue
                        _log("⏳ 等待刷脸二维码出现...")
                    else:
                        _log("⚠️ 未找到刷脸按钮, 截图: " + DEBUG_PNG)
                        face_clicked = True
                    page.wait_for_timeout(3000)
                    continue

                # 阶段2: 点完刷脸后等刷脸二维码出现
                if face_clicked and not face_qr_captured:
                    if capture_face_qr(page):
                        face_qr_captured = True
                        face_qr_last_capture = time.time()
                        _log("=" * 50)
                        _log(f"⏳ 第 2 步: 请用手机抖音 App 扫描刷脸二维码 {FACE_QR_FILE}")
                        _log("   扫码后在手机上完成刷脸验证")
                        _log("=" * 50)
                    else:
                        # 也可能直接登录成功(不需要二次扫码)
                        page.wait_for_timeout(1000)
                    continue

                # 阶段3: 刷脸二维码已抓取, 等用户扫码刷脸, 定期刷新截图
                if face_qr_captured:
                    # 每 60 秒重新抓一次刷脸二维码(防止过期)
                    if time.time() - face_qr_last_capture > 60:
                        if capture_face_qr(page):
                            face_qr_last_capture = time.time()
                            _log("📷 刷脸二维码已刷新, 请重新扫描 douyin_face_qrcode.png")
                    page.wait_for_timeout(2000)
                    continue

                # 登录二维码过期: 真正在网页上点击刷新(每 50 秒强制刷新)
                expired = False
                try:
                    body_text = page.inner_text("body", timeout=1000)
                    expired = any(k in body_text for k in
                                  ("二维码已失效", "二维码失效", "二维码已过期",
                                   "点击刷新", "重新扫码"))
                except Exception:
                    pass
                if expired or time.time() - last_qr_capture > 50:
                    _log("🔄 在网页上刷新登录二维码...")
                    if refresh_qr_on_page(page):
                        last_qr_capture = time.time()
                    else:
                        last_qr_capture = time.time()
                print(".", end="", flush=True)

            _log("\n⏰ 等待超时, 未登录")
            return 1
        finally:
            context.close()


def do_check():
    if not os.path.exists(PROFILE_DIR):
        _log(f"❌ 浏览器配置目录不存在: {PROFILE_DIR}")
        _log("   请先运行: python3 douyin_qrlogin.py")
        return 1
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        context = make_context(p)
        page = context.new_page()
        setup_page(page)
        try:
            goto_home(page)
            page.wait_for_timeout(4000)
            if is_logged_in(context):
                dy_id, nick = fetch_self_info(page)
                _log(f"✅ 登录态有效  抖音号: {dy_id or '未知'}  昵称: {nick or '未知'}")
                return 0
            _log("❌ 登录态已失效, 请重新运行: python3 douyin_qrlogin.py")
            return 1
        finally:
            context.close()


def main():
    parser = argparse.ArgumentParser(description="抖音网页版扫码登录(无头, 持久化)")
    parser.add_argument("--check", action="store_true", help="只检查登录态")
    parser.add_argument("--timeout", type=int, default=WAIT_SEC, help="等待秒数")
    args = parser.parse_args()

    try:
        if args.check:
            return do_check()
        return do_login(args.timeout)
    except ImportError:
        _log("❌ 缺少 playwright。请先安装:")
        _log("   pip3 install playwright")
        _log("   python3 -m playwright install chromium")
        return 3
    except Exception as e:
        _log(f"❌ 异常: {e}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
