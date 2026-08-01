package com.mediahub115.viewer;

import android.content.Context;
import android.util.Log;

import java.io.File;
import java.io.FileOutputStream;
import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.Locale;

/**
 * 面包屑日志（xgimi-debug 分支）：关键节点写文件，进程崩掉后最后一行
 * 就是死亡位置；下次启动 MainActivity 把内容弹出来（拍照回传）。
 */
public final class BootLog {

    private static final String LOG_FILE = "boot_log.txt";

    private BootLog() {
    }

    public static synchronized void log(Context ctx, String msg) {
        String line = new SimpleDateFormat("MM-dd HH:mm:ss.SSS", Locale.US)
                .format(new Date()) + "  " + msg + "\n";
        try (FileOutputStream f = ctx.openFileOutput(LOG_FILE, Context.MODE_APPEND)) {
            f.write(line.getBytes());
        } catch (Exception ignored) {
        }
        Log.i("MediaHub", msg);
    }

    /** 读取并返回全部日志；无日志返回 null。 */
    public static synchronized String read(Context ctx) {
        File f = ctx.getFileStreamPath(LOG_FILE);
        if (!f.exists()) {
            return null;
        }
        try (java.io.FileInputStream in = ctx.openFileInput(LOG_FILE)) {
            byte[] buf = new byte[(int) Math.min(f.length(), 8192)];
            int n = in.read(buf);
            return n > 0 ? new String(buf, 0, n) : null;
        } catch (Exception e) {
            return null;
        }
    }

    public static synchronized void clear(Context ctx) {
        ctx.getFileStreamPath(LOG_FILE).delete();
    }
}
