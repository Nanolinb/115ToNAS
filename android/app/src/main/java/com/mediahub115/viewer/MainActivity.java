package com.mediahub115.viewer;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.os.Build;
import android.os.Bundle;
import android.text.InputType;
import android.util.Log;
import android.view.KeyEvent;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.CheckBox;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

/**
 * Puffin Media · 观影端 TV 壳：
 * - WebView 加载 NAS 上的观影页（海报墙/搜索/过滤）
 * - 播放走内嵌 ExoPlayer（应用内全屏，电视/投影硬解，可选音轨/字幕，
 *   音轨解不了自动回落服务端 AAC 转码），不跳外部 App
 *
 * xgimi-debug 分支：极米 GMUI 白屏闪退排查
 * - 面包屑日志（BootLog）：每步写文件，进程崩掉后下次启动先弹出来
 * - onRenderProcessGone：WebView 渲染进程崩溃时重建而不是整 App 陪葬（API 26+）
 * - 服务器弹窗加「兼容模式」开关：WebView 改软件渲染，绕开部分 ROM 的 GPU 崩溃
 * - getDefaultVideoPoster 返回 1x1 占位图：老 WebView 默认实现返回 null，
 *   页面含无 poster 的 <video> 时其内部 getWidth() NPE 整 App 闪退（实锤根因）
 */
public class MainActivity extends Activity {

    private static final String PREFS = "mediahub";
    private static final String KEY_SERVER = "server";
    private static final String KEY_SOFT_RENDER = "soft_render";
    private static final String DEFAULT_SERVER = "http://192.168.1.107:8115";

    private WebView web;
    private SharedPreferences sp;

    private void log(String msg) {
        BootLog.log(this, msg);
    }

    // ---------- 生命周期 ----------

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        // Java 层未捕获异常：落盘后再死，下次启动能看到堆栈
        Thread.setDefaultUncaughtExceptionHandler((t, e) -> {
            log("CRASH[" + t.getName() + "] " + e + "\n" + Log.getStackTraceString(e));
            android.os.Process.killProcess(android.os.Process.myPid());
        });
        // App 在前台时常亮：挡住小米的广告屏保（用户反馈没播视频也很快进屏保广告）。
        // 退出 App 后交还系统，按系统设置的屏保超时走
        getWindow().addFlags(android.view.WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        sp = getSharedPreferences(PREFS, MODE_PRIVATE);

        // 上次启动留下了日志 = 上次没活到页面加载完成（崩了）→ 先弹出来给人看
        String lastLog = BootLog.read(this);
        if (lastLog != null) {
            BootLog.clear(this);
            showLastLogDialog(lastLog);
        } else {
            init();
        }
    }

    private void init() {
        log("init, sdk=" + Build.VERSION.SDK_INT + ", softRender=" + sp.getBoolean(KEY_SOFT_RENDER, false));
        setupWebView();

        String server = sp.getString(KEY_SERVER, "");
        if (server.isEmpty()) {
            showServerDialog(null);
        } else {
            loadServer(server);
        }
    }

    private final android.os.Handler handler =
            new android.os.Handler(android.os.Looper.getMainLooper());
    private Runnable loadWatchdog;

    /** 加载服务器首页 + 看门狗：12 秒没完事就当地址错了，停掉加载弹回输入框 */
    private void loadServer(String server) {
        log("loadUrl " + server);
        if (loadWatchdog != null) {
            handler.removeCallbacks(loadWatchdog);
        }
        loadWatchdog = () -> {
            web.stopLoading();
            log("load timeout");
            showServerDialog("连接超时：服务器地址错误或 NAS 不在线，请重新输入");
        };
        handler.postDelayed(loadWatchdog, 12000);
        web.loadUrl(server + "/");
    }

    private void setupWebView() {
        if (web != null) {
            web.destroy();
        }
        web = new WebView(this);
        log("webview created");
        // 兼容模式：软件渲染。极米等定制 ROM 的 WebView GPU 合成崩溃时勾上有效；
        // 只影响海报墙界面流畅度，视频播放是独立 ExoPlayer 硬解，不受影响
        if (sp.getBoolean(KEY_SOFT_RENDER, false)) {
            web.setLayerType(WebView.LAYER_TYPE_SOFTWARE, null);
            log("software layer on");
        }
        setContentView(web);
        // debug 包开启 WebView 远程调试（chrome://inspect），release 自动关闭
        if (BuildConfig.DEBUG) {
            WebView.setWebContentsDebuggingEnabled(true);
        }
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setTextZoom(100);
        // 禁用 WebView 磁盘缓存：服务端已是局域网毫秒级，缓存只会让电视端
        // 停留在旧版 JS/CSS（启发式缓存按 Last-Modified 10% 算新鲜期）
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        // 不锁视口宽度：1.2.2/1.2.3 的 width=1600 注入把 TV 布局打乱
        // （最近更新挤到底部、Hero 右偏），回滚到设备默认视口。
        // 4K 面板卡片偏小的问题以后在服务端 tv.css 里按 vw 解决，不动 App

        web.addJavascriptInterface(new NativeBridge(), "MediaHubNative");
        web.setWebChromeClient(new android.webkit.WebChromeClient() {
            @Override
            public android.graphics.Bitmap getDefaultVideoPoster() {
                // 老 WebView（极米 GMUI / Android 9）默认实现返回 null，
                // 页面含无 poster 的 <video> 时其内部 getWidth() NPE 整 App 闪退
                return android.graphics.Bitmap.createBitmap(
                        1, 1, android.graphics.Bitmap.Config.ARGB_8888);
            }
        });
        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                                        WebResourceError error) {
                if (request.isForMainFrame()) {
                    handler.removeCallbacks(loadWatchdog);
                    log("page error: " + error.getErrorCode() + " " + error.getDescription());
                    showServerDialog("服务器地址错误或无法连接（" + error.getDescription()
                            + "），请重新输入");
                }
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                // 活到这 = 本次启动没崩，撤看门狗、清面包屑
                handler.removeCallbacks(loadWatchdog);
                BootLog.clear(MainActivity.this);
            }

            @Override
            public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
                // 渲染进程崩溃默认会杀掉整个 App（白屏闪退的元凶之一）；
                // 返回 true 接管：重建 WebView 重新加载，App 不死
                log("renderer gone, didCrash=" + detail.didCrash());
                setupWebView();
                String server = sp.getString(KEY_SERVER, "");
                if (!server.isEmpty()) {
                    loadServer(server);
                }
                return true;
            }
        });
    }

    /** 上次崩溃的面包屑展示：看完后继续正常启动 */
    private void showLastLogDialog(String content) {
        ScrollView sv = new ScrollView(this);
        TextView tv = new TextView(this);
        tv.setText("上次启动中断，日志如下（拍照发给开发者）：\n\n" + content);
        tv.setTextSize(13);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        tv.setPadding(pad, pad, pad, pad);
        sv.addView(tv);
        new AlertDialog.Builder(this)
                .setTitle("启动诊断")
                .setView(sv)
                .setPositiveButton("继续启动", (d, w) -> init())
                .setCancelable(false)
                .show();
    }

    /** 服务器地址设置弹窗（首次启动 / 连接失败 / 按菜单键时弹出）。
     *  四段 IP + 端口分栏数字输入（九宫格）：前两段预填 192/168（可改，
     *  Tailscale 等网段直接改）；每段满 3 位、或两位已超 25（再加任何数字
     *  都超 255）自动跳下一段；端口预填 8115 一般不用动。
     *  地址连不上：12 秒看门狗或加载报错都会弹回本框并提示。 */
    private void showServerDialog(String err) {
        String[] oct = {"192", "168", "", ""};
        String port = "8115";
        java.util.regex.Matcher m = java.util.regex.Pattern.compile(
                "^https?://(\\d+)\\.(\\d+)\\.(\\d+)\\.(\\d+)(?::(\\d+))?/?$")
                .matcher(sp.getString(KEY_SERVER, ""));
        if (m.matches()) {
            for (int i = 0; i < 4; i++) {
                oct[i] = m.group(i + 1);
            }
            if (m.group(5) != null) {
                port = m.group(5);
            }
        }

        final EditText[] seg = new EditText[5];  // 0-3 = IP 四段，4 = 端口
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(android.view.Gravity.CENTER_VERTICAL);
        for (int i = 0; i < 5; i++) {
            final EditText e = new EditText(this);
            e.setInputType(InputType.TYPE_CLASS_NUMBER);
            e.setText(i < 4 ? oct[i] : port);
            e.setSelectAllOnFocus(true);
            seg[i] = e;
            row.addView(e, new LinearLayout.LayoutParams(0, -2, i == 4 ? 3f : 2.2f));
            if (i < 4) {
                final int idx = i;
                e.addTextChangedListener(new android.text.TextWatcher() {
                    public void beforeTextChanged(CharSequence s, int a, int b, int c) { }
                    public void onTextChanged(CharSequence s, int a, int b, int c) { }
                    public void afterTextChanged(android.text.Editable s) {
                        String v = s.toString();
                        if (v.length() == 3
                                || (v.length() == 2 && Integer.parseInt(v) > 25)) {
                            // post 延迟跳焦：事件处理中直接 requestFocus 会被
                            // 对话框默认按钮（连接）抢走焦点
                            final EditText next = seg[idx + 1];
                            next.post(next::requestFocus);
                        }
                    }
                });
                TextView sep = new TextView(this);
                sep.setText(i < 3 ? "." : ":");
                row.addView(sep);
            }
        }

        final CheckBox soft = new CheckBox(this);
        soft.setText("兼容模式（白屏闪退时勾选）");
        soft.setChecked(sp.getBoolean(KEY_SOFT_RENDER, false));
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.addView(row);
        box.addView(soft);
        AlertDialog dlg = new AlertDialog.Builder(this)
                .setTitle("NAS 服务器地址")
                .setMessage(err == null
                        ? "首次使用：输入 NAS 的 IP 与端口（默认 8115）"
                        : err + "\n\n请确认地址：")
                .setView(box)
                .setPositiveButton("连接", null)  // 点击逻辑 show 后接管：校验不过不关窗
                .setNegativeButton("取消", null)
                .setCancelable(false)
                .show();
        // 初始焦点放到第一个没填的 IP 段（post 同上防被默认按钮抢焦）
        if (oct[2].isEmpty()) {
            seg[2].post(seg[2]::requestFocus);
        } else if (oct[3].isEmpty()) {
            seg[3].post(seg[3]::requestFocus);
        }
        dlg.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(v -> {
            StringBuilder ip = new StringBuilder();
            for (int i = 0; i < 4; i++) {
                int n;
                try {
                    n = Integer.parseInt(seg[i].getText().toString().trim());
                } catch (NumberFormatException ex) {
                    n = -1;
                }
                if (n < 0 || n > 255) {
                    android.widget.Toast.makeText(this,
                            "IP 第 " + (i + 1) + " 段不对（0-255）",
                            android.widget.Toast.LENGTH_SHORT).show();
                    seg[i].requestFocus();
                    return;
                }
                if (i > 0) {
                    ip.append('.');
                }
                ip.append(n);
            }
            String p = seg[4].getText().toString().trim();
            if (p.isEmpty()) {
                p = "8115";
            }
            String url = "http://" + ip + ":" + p;
            sp.edit().putString(KEY_SERVER, url)
                    .putBoolean(KEY_SOFT_RENDER, soft.isChecked()).apply();
            dlg.dismiss();
            // 软渲染开关变了要重建 WebView 才生效
            setupWebView();
            loadServer(url);
        });
    }

    @Override
    protected void onResume() {
        super.onResume();
        // 原生播放器返回：进度/看完标记可能变了，通知网页重渲「继续观看/最近更新」
        if (web != null) {
            web.evaluateJavascript(
                    "window.tvProgressChanged && window.tvProgressChanged()", null);
        }
    }

    @Override
    public boolean onKeyDown(int keyCode, KeyEvent event) {
        // 遥控器「菜单」键：重新设置服务器地址
        if (keyCode == KeyEvent.KEYCODE_MENU) {
            showServerDialog(null);
            return true;
        }
        return super.onKeyDown(keyCode, event);
    }

    @Override
    public void onBackPressed() {
        // 先让网页逐层关（播放器 → 详情弹窗）；网页没东西可关再弹退出确认，防误退
        web.evaluateJavascript(
                "(window.tvBack && window.tvBack()) ? '1' : '0'", v -> {
                    if (v != null && v.contains("1")) {
                        return;
                    }
                    if (web.canGoBack()) {
                        web.goBack();
                        return;
                    }
                    new AlertDialog.Builder(this)
                            .setTitle("退出")
                            .setMessage("确定退出 Puffin Media？")
                            .setPositiveButton("退出", (d, w) -> finish())
                            .setNegativeButton("取消", null)
                            .show();
                });
    }

    /** 网页通过 window.MediaHubNative 调用的原生能力 */
    public class NativeBridge {

        /**
         * 内嵌 ExoPlayer 播放（应用内全屏）：电视/投影硬件解码，
         * 遥控器可选音轨/字幕/选集；音轨解不了自动回落服务端 AAC 转码。
         * subsJson: [{"label","url","ext"}]，epsJson: [{"id","label","name"}]，可为 null。
         */
        @android.webkit.JavascriptInterface
        public void play(final String url, final String title, final String subsJson,
                         final String epsJson) {
            runOnUiThread(() -> {
                android.content.Intent i = new android.content.Intent(
                        MainActivity.this, PlayerActivity.class);
                i.putExtra("url", url);
                i.putExtra("title", title);
                i.putExtra("subs", subsJson);
                i.putExtra("eps", epsJson);
                // 观影端设了密码时，原生侧请求（换集/字幕/转码流）也要带会话 Cookie
                String server = sp.getString(KEY_SERVER, DEFAULT_SERVER);
                i.putExtra("cookie",
                        android.webkit.CookieManager.getInstance().getCookie(server));
                startActivity(i);
            });
        }

        /** 旧版网页缓存可能还在调两参/三参版本，兼容转发。 */
        @android.webkit.JavascriptInterface
        public void play(final String url, final String title, final String subsJson) {
            play(url, title, subsJson, null);
        }

        @android.webkit.JavascriptInterface
        public void play(final String url, final String title) {
            play(url, title, null, null);
        }

        /** 观影端首页「继续观看」栏：原生播放器的续播存档（pos_<id> → 毫秒） */
        @android.webkit.JavascriptInterface
        public String getResume() {
            try {
                return new org.json.JSONObject(
                        getSharedPreferences("resume", MODE_PRIVATE).getAll()).toString();
            } catch (Exception e) {
                return "{}";
            }
        }
    }
}
