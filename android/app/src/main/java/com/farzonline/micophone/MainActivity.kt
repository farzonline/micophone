package com.farzonline.micophone

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.View
import android.widget.TextView
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.isVisible
import androidx.core.widget.doAfterTextChanged
import com.google.android.material.button.MaterialButton
import com.google.android.material.button.MaterialButtonToggleGroup
import com.google.android.material.color.MaterialColors
import com.google.android.material.dialog.MaterialAlertDialogBuilder
import com.google.android.material.materialswitch.MaterialSwitch
import com.google.android.material.progressindicator.LinearProgressIndicator
import com.google.android.material.textfield.TextInputEditText
import com.google.android.material.textfield.TextInputLayout
import kotlin.concurrent.thread
import kotlin.math.log10

class MainActivity : AppCompatActivity() {

    private lateinit var mode: MaterialButtonToggleGroup
    private lateinit var wifiGroup: View
    private lateinit var usbHelp: View
    private lateinit var hostLayout: TextInputLayout
    private lateinit var host: TextInputEditText
    private lateinit var find: MaterialButton
    private lateinit var portLayout: TextInputLayout
    private lateinit var port: TextInputEditText
    private lateinit var voice: MaterialSwitch
    private lateinit var toggle: MaterialButton
    private lateinit var mute: MaterialButton
    private lateinit var level: LinearProgressIndicator
    private lateinit var status: TextView

    private val prefs by lazy { getSharedPreferences("settings", MODE_PRIVATE) }
    private val handler = Handler(Looper.getMainLooper())
    private var pcName: String? = null
    private var shownLevel = 0f

    /** 20 Hz level meter: dB scale, fast attack, slow release. */
    private val meter = object : Runnable {
        override fun run() {
            shownLevel = maxOf(StreamService.takeLevel(), shownLevel * 0.85f)
            val db = 20 * log10(maxOf(shownLevel, 1e-4f))
            level.setProgressCompat(((db + 60) / 60 * 100).toInt().coerceIn(0, 100), false)
            handler.postDelayed(this, 50)
        }
    }

    private val askPermissions =
        registerForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { granted ->
            when {
                granted[Manifest.permission.RECORD_AUDIO] == true -> start()
                !shouldShowRequestPermissionRationale(Manifest.permission.RECORD_AUDIO) -> permissionSettings()
                else -> status.setText(R.string.err_permission)
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)
        mode = findViewById(R.id.mode)
        wifiGroup = findViewById(R.id.wifi_group)
        usbHelp = findViewById(R.id.usb_help)
        hostLayout = findViewById(R.id.host_layout)
        host = findViewById(R.id.host)
        find = findViewById(R.id.find)
        portLayout = findViewById(R.id.port_layout)
        port = findViewById(R.id.port)
        voice = findViewById(R.id.voice)
        toggle = findViewById(R.id.toggle)
        mute = findViewById(R.id.mute)
        level = findViewById(R.id.level)
        status = findViewById(R.id.status)

        mode.check(if (prefs.getBoolean("usb", false)) R.id.mode_usb else R.id.mode_wifi)
        host.setText(prefs.getString("host", ""))
        pcName = prefs.getString("pc_name", null)
        port.setText(prefs.getInt("port", Protocol.DEFAULT_PORT).toString())
        voice.isChecked = prefs.getBoolean("voice", true)
        showPcName()

        mode.addOnButtonCheckedListener { _, _, _ -> render() }
        host.doAfterTextChanged {
            hostLayout.error = null
            if (it.toString() != prefs.getString("host", "")) {
                pcName = null // typed by hand: we don't know the PC's name yet
                showPcName()
            }
        }
        port.doAfterTextChanged { portLayout.error = null }
        find.setOnClickListener { findPcs(auto = false) }
        toggle.setOnClickListener {
            if (StreamService.running) stopService(Intent(this, StreamService::class.java)) else start()
        }
        mute.setOnClickListener {
            startService(Intent(this, StreamService::class.java).setAction(StreamService.ACTION_MUTE))
        }

        if (savedInstanceState == null && !isUsb() && host.text.isNullOrBlank()) findPcs(auto = true)
    }

    override fun onResume() {
        super.onResume()
        StreamService.listener = ::render
        render()
        handler.post(meter)
    }

    override fun onPause() {
        StreamService.listener = null
        handler.removeCallbacks(meter)
        super.onPause()
    }

    private fun isUsb() = mode.checkedButtonId == R.id.mode_usb

    private fun portValue(): Int? {
        val p = port.text.toString().toIntOrNull()
        if (p == null || p !in 1..65535) {
            portLayout.error = getString(R.string.err_port)
            return null
        }
        return p
    }

    private fun findPcs(auto: Boolean) {
        val p = portValue() ?: return
        find.isEnabled = false
        find.setText(R.string.searching)
        thread {
            val pcs = runCatching { Protocol.discover(applicationContext, p) }.getOrDefault(emptyList())
            runOnUiThread {
                if (isDestroyed) return@runOnUiThread
                find.setText(R.string.find_pc)
                find.isEnabled = !StreamService.running
                when {
                    pcs.size == 1 -> choose(pcs[0])
                    pcs.size > 1 -> MaterialAlertDialogBuilder(this)
                        .setTitle(R.string.choose_pc)
                        .setItems(pcs.map { "${it.name}  (${it.address.hostAddress})" }.toTypedArray()) { _, i ->
                            choose(pcs[i])
                        }
                        .show()
                    !auto -> MaterialAlertDialogBuilder(this)
                        .setTitle(R.string.no_pc_title)
                        .setMessage(R.string.no_pc_body)
                        .setPositiveButton(android.R.string.ok, null)
                        .show()
                }
            }
        }
    }

    private fun choose(pc: Protocol.Pc) {
        host.setText(pc.address.hostAddress)
        pcName = pc.name // after setText: its watcher clears the name
        showPcName()
    }

    private fun showPcName() {
        hostLayout.helperText = pcName?.let { getString(R.string.pc_found, it) }
    }

    private fun start() {
        val p = portValue() ?: return
        val usb = isUsb()
        val ip = host.text.toString().trim()
        if (!usb && ip.isEmpty()) {
            hostLayout.error = getString(R.string.err_no_pc)
            return
        }
        val wanted = mutableListOf(Manifest.permission.RECORD_AUDIO)
        if (Build.VERSION.SDK_INT >= 33) wanted += Manifest.permission.POST_NOTIFICATIONS
        val missing = wanted.filter { checkSelfPermission(it) != PackageManager.PERMISSION_GRANTED }
        if (Manifest.permission.RECORD_AUDIO in missing) {
            askPermissions.launch(missing.toTypedArray())
            return
        }

        prefs.edit()
            .putBoolean("usb", usb)
            .putString("host", ip)
            .putString("pc_name", pcName)
            .putInt("port", p)
            .putBoolean("voice", voice.isChecked)
            .apply()
        startForegroundService(
            Intent(this, StreamService::class.java)
                .putExtra(StreamService.EXTRA_HOST, ip)
                .putExtra(StreamService.EXTRA_PC_NAME, pcName)
                .putExtra(StreamService.EXTRA_PORT, p)
                .putExtra(StreamService.EXTRA_USB, usb)
                .putExtra(StreamService.EXTRA_VOICE, voice.isChecked)
        )
    }

    private fun permissionSettings() {
        MaterialAlertDialogBuilder(this)
            .setTitle(R.string.permission_title)
            .setMessage(R.string.permission_body)
            .setPositiveButton(R.string.open_settings) { _, _ ->
                startActivity(
                    Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.fromParts("package", packageName, null))
                )
            }
            .setNegativeButton(android.R.string.cancel, null)
            .show()
    }

    private fun render() {
        val running = StreamService.running
        val usb = isUsb()
        wifiGroup.isVisible = !usb
        usbHelp.isVisible = usb
        for (i in 0 until mode.childCount) mode.getChildAt(i).isEnabled = !running
        listOf(host, find, port, voice).forEach { it.isEnabled = !running }

        toggle.setText(if (running) R.string.stop else R.string.start)
        toggle.setIconResource(if (running) R.drawable.ic_stop else R.drawable.ic_mic)
        mute.isVisible = running
        mute.setText(if (StreamService.muted) R.string.unmute else R.string.mute)
        mute.setIconResource(if (StreamService.muted) R.drawable.ic_mic else R.drawable.ic_mic_off)
        level.visibility = if (running) View.VISIBLE else View.INVISIBLE

        val text = StreamService.status.ifEmpty { getString(R.string.ready) }
        status.text = if (StreamService.muted) getString(R.string.muted_suffix, text) else text
        status.setTextColor(
            MaterialColors.getColor(
                status,
                if (StreamService.connected) androidx.appcompat.R.attr.colorPrimary
                else com.google.android.material.R.attr.colorOnSurfaceVariant
            )
        )
    }
}
