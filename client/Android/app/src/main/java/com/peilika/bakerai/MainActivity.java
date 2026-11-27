package com.peilika.bakerai;

import android.os.Bundle;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebView;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.core.view.WindowInsetsControllerCompat;

import com.getcapacitor.BridgeActivity;

public class MainActivity extends BridgeActivity {

    /**
     * 最近一次算出来的安全区（CSS 像素 = dp），顺序：上 / 右 / 下 / 左。
     *
     * 为什么要自己算：Android 15 起，targetSdk 35+ 的应用被强制「边到边」，
     * 网页会一直画到状态栏 / 挖孔底下；而安卓 WebView 的
     * env(safe-area-inset-top) 只反映刘海，**拿不到状态栏高度**，
     * 所以顶部页签会压到时间、电量那一条上。这里用 WindowInsets 量出
     * 系统栏与网页的真实重叠量，再写进网页的 --safe-top / --safe-bottom。
     */
    private final float[] safe = new float[4];

    /** 上一次推给网页的值：没变就不重复注入，省得每帧 evaluateJavascript */
    private final float[] pushed = new float[] { -1f, -1f, -1f, -1f };

    /**
     * 本地插件（不在 npm 依赖里，capacitor.plugins.json 不会自动带上），
     * 必须在 super.onCreate 之前注册，Capacitor 才会把方法表注入 WebView。
     *
     * · SaveFile：导出/截图落地（MediaStore → 下载/Baker AI）
     * · QqGroup：关于页点群二维码 → 唤起手机 QQ 群名片
     */
    @Override
    public void onCreate(Bundle savedInstanceState) {
        registerPlugin(SaveFilePlugin.class);
        registerPlugin(QqGroupPlugin.class);
        super.onCreate(savedInstanceState);
        setupSafeArea();
    }

    // =========================================================================
    // 安全区（异形屏 / 状态栏）
    // =========================================================================

    private void setupSafeArea() {
        // 状态栏、导航栏图标固定浅色：应用整体是深色底，别跟着系统深浅色走
        WindowInsetsControllerCompat controller =
                WindowCompat.getInsetsController(getWindow(), getWindow().getDecorView());
        controller.setAppearanceLightStatusBars(false);
        controller.setAppearanceLightNavigationBars(false);

        WebView webView = getBridge() == null ? null : getBridge().getWebView();
        if (webView == null) return;

        // 网页启动脚本会同步读这个接口（见 index.html），拿不到时按 0 处理
        webView.addJavascriptInterface(new InsetsBridge(), "BakerInsets");

        // 旋转、进出全屏、系统栏显示/隐藏都会重新分发 insets，收到就重算一次
        View decor = getWindow().getDecorView();
        ViewCompat.setOnApplyWindowInsetsListener(decor, (view, windowInsets) -> {
            view.post(this::refreshSafeArea);
            return windowInsets; // 不消费，IME 等交给系统 / Capacitor 自己处理
        });
        ViewCompat.requestApplyInsets(decor);
        decor.post(this::refreshSafeArea);
    }

    /** 量一次安全区并推给网页（必须在 UI 线程） */
    private void refreshSafeArea() {
        WebView webView = getBridge() == null ? null : getBridge().getWebView();
        if (webView == null) return;

        View decor = getWindow().getDecorView();
        WindowInsetsCompat insets = ViewCompat.getRootWindowInsets(decor);
        // 还没布局完（尺寸为 0）时先不算：此时量出来的是假的，会把 0 写死
        if (insets == null || !webView.isAttachedToWindow() || webView.getHeight() == 0) return;

        Insets bars = insets.getInsets(
                WindowInsetsCompat.Type.systemBars() | WindowInsetsCompat.Type.displayCutout());

        int[] wvLoc = new int[2];
        webView.getLocationOnScreen(wvLoc);
        int[] decorLoc = new int[2];
        decor.getLocationOnScreen(decorLoc);

        // 系统栏边界与网页边界都换算到「窗口坐标」里，重叠多少就补多少：
        //   · 边到边（Android 15+ 强制）：网页从 0 起 → 重叠 = 状态栏高度
        //   · 老系统由窗口自己让开：网页已经在状态栏下面 → 重叠 = 0
        // 这样同一份代码在新旧系统上都不会多补一截。
        int wvTop = wvLoc[1] - decorLoc[1];
        int wvLeft = wvLoc[0] - decorLoc[0];
        int wvBottom = wvTop + webView.getHeight();
        int wvRight = wvLeft + webView.getWidth();

        float topPx = Math.max(0, bars.top - wvTop);
        float leftPx = Math.max(0, bars.left - wvLeft);
        float bottomPx = Math.max(0, wvBottom - (decor.getHeight() - bars.bottom));
        float rightPx = Math.max(0, wvRight - (decor.getWidth() - bars.right));

        // 物理像素 → CSS 像素（WebView 里 1 CSS px = 1dp）
        float density = getResources().getDisplayMetrics().density;
        synchronized (safe) {
            safe[0] = topPx / density;
            safe[1] = rightPx / density;
            safe[2] = bottomPx / density;
            safe[3] = leftPx / density;
        }
        pushSafeArea(webView);
    }

    /** 把 safe[] 写进网页的 CSS 变量 */
    private void pushSafeArea(WebView webView) {
        float top;
        float right;
        float bottom;
        float left;
        synchronized (safe) {
            top = safe[0];
            right = safe[1];
            bottom = safe[2];
            left = safe[3];
        }
        if (Math.abs(top - pushed[0]) < 0.5f
                && Math.abs(right - pushed[1]) < 0.5f
                && Math.abs(bottom - pushed[2]) < 0.5f
                && Math.abs(left - pushed[3]) < 0.5f) {
            return;
        }
        pushed[0] = top;
        pushed[1] = right;
        pushed[2] = bottom;
        pushed[3] = left;

        // 顺手广播一个事件：网页里若已经渲染完，可以立刻重排
        String js = "document.documentElement.style.setProperty('--safe-top','" + top + "px');"
                + "document.documentElement.style.setProperty('--safe-right','" + right + "px');"
                + "document.documentElement.style.setProperty('--safe-bottom','" + bottom + "px');"
                + "document.documentElement.style.setProperty('--safe-left','" + left + "px');"
                + "window.dispatchEvent(new Event('baker:insets'));";
        webView.evaluateJavascript(js, null);
    }

    /**
     * 给网页同步读的安全区接口。
     *
     * 网页在 <head> 里就要知道该让开多少，这时 DOM 还没加载完、也来不及等
     * evaluateJavascript，所以走 addJavascriptInterface 同步取值。
     */
    private final class InsetsBridge {

        @JavascriptInterface
        public float getTop() {
            synchronized (safe) {
                return safe[0];
            }
        }

        @JavascriptInterface
        public float getRight() {
            synchronized (safe) {
                return safe[1];
            }
        }

        @JavascriptInterface
        public float getBottom() {
            synchronized (safe) {
                return safe[2];
            }
        }

        @JavascriptInterface
        public float getLeft() {
            synchronized (safe) {
                return safe[3];
            }
        }
    }
}
