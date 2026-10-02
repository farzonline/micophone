package com.farzonline.micophone

import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.graphics.drawable.Icon
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.media.audiofx.AudioEffect
import android.media.audiofx.NoiseSuppressor
import android.net.wifi.WifiManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.Process
import android.os.SystemClock
import android.provider.Settings
import java.io.DataInputStream
import java.io.IOException
import java.io.OutputStream
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import kotlin.concurrent.thread
import kotlin.math.abs

/**
 * Captures the mic and streams it to the Micophone PC app (pc/receiver.py).
 * Wi-Fi = UDP with 1 s HELLO/ACK keepalives; USB = TCP to 127.0.0.1, tunnelled by
 * `adb reverse` (the PC app sets that up). Both links reconnect on their own.
 */
class StreamService : Service() {

    companion object {
        const val ACTION_STOP = "com.farzonline.micophone.action.STOP"
        const val ACTION_MUTE = "com.farzonline.micophone.action.MUTE"
        const val EXTRA_HOST = "host"
        const val EXTRA_PC_NAME = "pc_name"
        const val EXTRA_PORT = "port"
        const val EXTRA_USB = "usb"
        const val EXTRA_VOICE = "voice"
        private const val CHANNEL = "stream"
        private const val NOTIFICATION_ID = 1

        // UI state. Written on the main thread, except `level` (audio thread).
        var running = false
            private set
        var connected = false
            private set
        var status = ""
            private set
        @Volatile var muted = false
            private set
        @Volatile private var level = 0f
        var listener: (() -> Unit)? = null

        /** Peak level since the last call, 0..1. */
        fun takeLevel(): Float = level.also { level = 0f }

        private val main = Handler(Looper.getMainLooper())
    }

    @Volatile private var active = false
    @Volatile private var link: Link? = null
    @Volatile private var failure: String? = null
    private var worker: Thread? = null
    private var wifiLock: WifiManager.WifiLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_MUTE -> {
                muted = !muted
                publish(connected, status)
                return START_NOT_STICKY
            }
        }
        if (intent == null || worker != null) return START_NOT_STICKY // double tap
        val host = intent.getStringExtra(EXTRA_HOST) ?: return START_NOT_STICKY
        val pcName = intent.getStringExtra(EXTRA_PC_NAME)
        val port = intent.getIntExtra(EXTRA_PORT, Protocol.DEFAULT_PORT)
        val usb = intent.getBooleanExtra(EXTRA_USB, false)
        val voice = intent.getBooleanExtra(EXTRA_VOICE, true)

        muted = false
        running = true
        status = ""
        createChannel()
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            startForeground(NOTIFICATION_ID, notification(), ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE)
        } else {
            startForeground(NOTIFICATION_ID, notification())
        }
        if (!usb) acquireWifiLock()
        listener?.invoke()

        active = true
        worker = thread(name = "micophone-audio") { stream(host, pcName, port, usb, voice) }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        active = false
        link?.close() // unblocks a socket write so the worker can exit
        worker?.join(1500) // release the mic before a quick restart grabs it again
        wifiLock?.release()
        running = false
        connected = false
        muted = false
        level = 0f
        status = failure ?: ""
        listener?.invoke()
        super.onDestroy()
    }

    @SuppressLint("MissingPermission") // MainActivity checks RECORD_AUDIO before starting us
    private fun stream(host: String, pcName: String?, port: Int, usb: Boolean, voice: Boolean) {
        Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO)
        val rec = openRecorder(voice) ?: return fail(getString(R.string.err_mic_unavailable))
        val effects = if (voice) noiseSuppressor(rec.audioSessionId) else emptyList()
        val name = deviceName()
        val l = if (usb) UsbLink(this, port, rec.sampleRate, name, ::publish)
        else WifiLink(this, host, pcName, port, rec.sampleRate, name, ::publish)
        link = l
        try {
            rec.startRecording()
            if (rec.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                return fail(getString(R.string.err_mic_busy))
            }
            val buf = ByteArray(rec.sampleRate / 100 * 2) // 10 ms of 16-bit mono
            while (active) {
                val n = rec.read(buf, 0, buf.size) // blocks ~10 ms
                if (n < 0) return fail(getString(R.string.err_mic_read, n))
                if (muted) buf.fill(0, 0, n) // keep streaming silence: the PC stays connected
                level = maxOf(level, peak(buf, n))
                l.send(buf, n)
            }
        } finally {
            l.close()
            effects.forEach { it.release() }
            if (rec.recordingState == AudioRecord.RECORDSTATE_RECORDING) rec.stop()
            rec.release()
        }
    }

    /** 48 kHz matches Windows' native rate (no resampling anywhere); 44.1 kHz is the guaranteed fallback. */
    @SuppressLint("MissingPermission")
    private fun openRecorder(voice: Boolean): AudioRecord? {
        // VOICE_COMMUNICATION runs the phone's call-quality pipeline (noise suppression and,
        // on many phones, multi-mic beamforming). MIC is the least processed.
        val source = if (voice) MediaRecorder.AudioSource.VOICE_COMMUNICATION else MediaRecorder.AudioSource.MIC
        for (rate in intArrayOf(48000, 44100)) {
            val min = AudioRecord.getMinBufferSize(rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT)
            if (min <= 0) continue
            val rec = try {
                AudioRecord(source, rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT,
                    maxOf(min, rate / 100 * 2 * 4))
            } catch (e: RuntimeException) {
                continue
            }
            if (rec.state == AudioRecord.STATE_INITIALIZED) return rec
            rec.release()
        }
        return null
    }

    private fun noiseSuppressor(session: Int): List<AudioEffect> =
        if (!NoiseSuppressor.isAvailable()) emptyList()
        else listOfNotNull(runCatching { NoiseSuppressor.create(session)?.apply { enabled = true } }.getOrNull())

    private fun peak(buf: ByteArray, n: Int): Float {
        var max = 0
        var i = 0
        while (i + 1 < n) {
            val s = (buf[i + 1].toInt() shl 8) or (buf[i].toInt() and 0xFF)
            max = maxOf(max, abs(s))
            i += 2
        }
        return max / 32768f
    }

    private fun deviceName(): String =
        Settings.Global.getString(contentResolver, Settings.Global.DEVICE_NAME) ?: Build.MODEL

    private fun fail(msg: String) {
        failure = msg
        stopSelf()
    }

    /** Called from link threads; hops to the main thread for UI and notification. */
    private fun publish(isConnected: Boolean, text: String) {
        main.post {
            if (!running) return@post
            connected = isConnected
            status = text
            getSystemService(NotificationManager::class.java).notify(NOTIFICATION_ID, notification())
            listener?.invoke()
        }
    }

    @Suppress("DEPRECATION") // HIGH_PERF is the pre-Android-10 equivalent
    private fun acquireWifiLock() {
        val mode = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            WifiManager.WIFI_MODE_FULL_LOW_LATENCY
        } else {
            WifiManager.WIFI_MODE_FULL_HIGH_PERF
        }
        val wm = applicationContext.getSystemService(WIFI_SERVICE) as WifiManager
        wifiLock = wm.createWifiLock(mode, "micophone").apply {
            setReferenceCounted(false)
            acquire()
        }
    }

    private fun createChannel() {
        getSystemService(NotificationManager::class.java).createNotificationChannel(
            NotificationChannel(CHANNEL, getString(R.string.channel_name), NotificationManager.IMPORTANCE_LOW)
        )
    }

    private fun notification(): Notification {
        fun service(action: String, code: Int) = PendingIntent.getService(
            this, code, Intent(this, StreamService::class.java).setAction(action), PendingIntent.FLAG_IMMUTABLE
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE
        )
        val icon = if (muted) R.drawable.ic_mic_off else R.drawable.ic_mic
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(icon)
            .setContentTitle(getString(if (muted) R.string.notif_title_muted else R.string.notif_title))
            .setContentText(status)
            .setContentIntent(open)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(Notification.CATEGORY_SERVICE)
            .addAction(
                Notification.Action.Builder(
                    Icon.createWithResource(this, if (muted) R.drawable.ic_mic else R.drawable.ic_mic_off),
                    getString(if (muted) R.string.unmute else R.string.mute), service(ACTION_MUTE, 1)
                ).build()
            )
            .addAction(
                Notification.Action.Builder(
                    Icon.createWithResource(this, R.drawable.ic_stop), getString(R.string.stop), service(ACTION_STOP, 2)
                ).build()
            )
            .build()
    }
}

private interface Link {
    /** Called every 10 ms from the audio thread. Must never block for long. */
    fun send(pcm: ByteArray, n: Int)
    fun close()
}

private const val ACK_TIMEOUT_MS = 2500L

/**
 * UDP to the PC. HELLO every second; the PC's ACK proves it's hearing us. With no ACK the
 * socket is rebuilt (the network may have changed) and, if we know the PC by name, a
 * DISCOVER broadcast finds its new address.
 */
private class WifiLink(
    private val ctx: Context,
    private val host: String,
    @Volatile private var pcName: String?,
    private val port: Int,
    rate: Int,
    deviceName: String,
    private val onStatus: (Boolean, String) -> Unit,
) : Link {
    @Volatile private var target: InetAddress? = null
    @Volatile private var sock: DatagramSocket? = null
    @Volatile private var lastAck = 0L
    @Volatile private var busy = false
    private var everAcked = false
    private var openedAt = 0L
    private var nextOpen = 0L
    private var nextTick = 0L
    private var seq = 0
    private val packet = ByteArray(Protocol.HEADER + rate / 100 * 2)
    private val hello = Protocol.hello(rate, deviceName)

    override fun send(pcm: ByteArray, n: Int) {
        val now = SystemClock.elapsedRealtime()
        val s = sock ?: open(now) ?: return
        try {
            if (now >= nextTick) {
                nextTick = now + 1000
                tick(s, now)
            }
            val t = target ?: return
            Protocol.writeAudioHeader(packet, seq)
            System.arraycopy(pcm, 0, packet, Protocol.HEADER, n)
            s.send(DatagramPacket(packet, Protocol.HEADER + n, t, port))
            seq = (seq + 1) and 0xFFFF
        } catch (e: IOException) {
            close() // network went away; reopen in a second
        }
    }

    private fun tick(s: DatagramSocket, now: Long) {
        val acked = now - lastAck < ACK_TIMEOUT_MS
        val label = pcName ?: host
        if (acked) {
            everAcked = true
            onStatus(!busy, ctx.getString(if (busy) R.string.st_busy else R.string.st_connected, label))
        } else {
            onStatus(false, ctx.getString(if (everAcked) R.string.st_lost else R.string.st_looking, label))
            if (now - openedAt > 4000) {
                // Maybe a stale socket after a network switch: rebuild it before saying
                // HELLO, so the PC never sees two sockets from us at once.
                close()
                nextOpen = 0
                return
            }
            if (pcName != null) Protocol.broadcast(ctx, s, port) // PC may have a new IP
        }
        val t = target ?: resolve() ?: return
        s.send(DatagramPacket(hello, hello.size, t, port))
    }

    private fun resolve(): InetAddress? = try {
        InetAddress.getByName(host).also { target = it }
    } catch (e: IOException) {
        onStatus(false, ctx.getString(R.string.st_bad_host, host))
        null
    }

    private fun open(now: Long): DatagramSocket? {
        if (now < nextOpen) return null
        nextOpen = now + 1000
        return try {
            DatagramSocket().also { s ->
                Protocol.wifiNetwork(ctx)?.bindSocket(s)
                s.broadcast = true
                sock = s
                openedAt = now
                nextTick = 0
                thread(name = "micophone-rx", isDaemon = true) { readLoop(s) }
            }
        } catch (e: IOException) {
            null
        }
    }

    private fun readLoop(s: DatagramSocket) {
        val buf = ByteArray(256)
        val p = DatagramPacket(buf, buf.size)
        while (!s.isClosed) {
            try {
                p.length = buf.size
                s.receive(p)
            } catch (e: IOException) {
                return
            }
            val (type, name) = Protocol.parse(buf, p.length) ?: continue
            when (type) {
                Protocol.ACK, Protocol.BUSY -> if (p.address == target) {
                    lastAck = SystemClock.elapsedRealtime()
                    busy = type == Protocol.BUSY
                    pcName = name
                }
                Protocol.ANNOUNCE -> if (name == pcName) target = p.address
            }
        }
    }

    override fun close() {
        sock?.close()
        sock = null
    }
}

/** TCP to 127.0.0.1, which `adb reverse` forwards to the PC. Retries every second. */
private class UsbLink(
    private val ctx: Context,
    private val port: Int,
    rate: Int,
    deviceName: String,
    private val onStatus: (Boolean, String) -> Unit,
) : Link {
    @Volatile private var sock: Socket? = null
    @Volatile private var out: OutputStream? = null
    @Volatile private var lastAck = 0L
    @Volatile private var busy = false
    @Volatile private var pcName = "PC"
    private var nextConnect = 0L
    private var nextTick = 0L
    private val hello = Protocol.usbHello(rate, deviceName)

    override fun send(pcm: ByteArray, n: Int) {
        val now = SystemClock.elapsedRealtime()
        if (out == null && now >= nextConnect) connect(now)
        if (now >= nextTick) {
            nextTick = now + 1000
            val acked = out != null && now - lastAck < ACK_TIMEOUT_MS
            when {
                !acked -> onStatus(false, ctx.getString(R.string.st_usb_waiting))
                busy -> onStatus(false, ctx.getString(R.string.st_busy, pcName))
                else -> onStatus(true, ctx.getString(R.string.st_connected_usb, pcName))
            }
        }
        try {
            out?.write(pcm, 0, n)
        } catch (e: IOException) {
            close()
        }
    }

    private fun connect(now: Long) {
        nextConnect = now + 1000
        val s = Socket()
        try {
            s.tcpNoDelay = true
            s.connect(InetSocketAddress(InetAddress.getLoopbackAddress(), port), 300)
            s.getOutputStream().write(hello)
        } catch (e: IOException) {
            s.close() // no tunnel yet: cable out, USB debugging off, or PC app closed
            return
        }
        sock = s
        out = s.getOutputStream()
        thread(name = "micophone-rx", isDaemon = true) { readLoop(s) }
    }

    /** PC sends type:u8 + len:u8 + name each second. EOF = PC side gone. */
    private fun readLoop(s: Socket) {
        try {
            val input = DataInputStream(s.getInputStream())
            while (true) {
                val type = input.readUnsignedByte().toByte()
                val name = ByteArray(input.readUnsignedByte()).also { input.readFully(it) }
                lastAck = SystemClock.elapsedRealtime()
                busy = type == Protocol.BUSY
                pcName = String(name, Charsets.UTF_8)
            }
        } catch (e: IOException) {
            s.close() // makes the next write fail -> reconnect
        }
    }

    override fun close() {
        sock?.close()
        sock = null
        out = null
    }
}
