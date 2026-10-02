package com.farzonline.micophone

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.SystemClock
import java.io.IOException
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.SocketTimeoutException
import java.nio.ByteBuffer
import java.nio.ByteOrder

/** Wire protocol shared with pc/receiver.py (see its docstring). Keep both in sync. */
object Protocol {
    const val DEFAULT_PORT = 50005
    const val AUDIO: Byte = 1
    const val DISCOVER: Byte = 2
    const val ANNOUNCE: Byte = 3
    const val HELLO: Byte = 4
    const val ACK: Byte = 5
    const val BUSY: Byte = 6
    const val HEADER = 5 // "PM" + type + seq:u16le, in front of every UDP audio packet

    private val MAGIC = byteArrayOf('P'.code.toByte(), 'M'.code.toByte())

    data class Pc(val name: String, val address: InetAddress)

    fun msg(type: Byte, body: ByteArray = ByteArray(0)) = MAGIC + type + body

    fun hello(rate: Int, name: String) = msg(HELLO, le32(rate) + nameBytes(name))

    /** USB/TCP variant: the name is length-prefixed because raw PCM follows it. */
    fun usbHello(rate: Int, name: String): ByteArray {
        val n = nameBytes(name)
        return msg(HELLO, le32(rate) + n.size.toByte() + n)
    }

    /** (type, utf8 body) of a UDP control message, or null if it isn't ours. */
    fun parse(data: ByteArray, len: Int): Pair<Byte, String>? {
        if (len < 3 || data[0] != MAGIC[0] || data[1] != MAGIC[1]) return null
        return data[2] to String(data, 3, len - 3, Charsets.UTF_8)
    }

    fun writeAudioHeader(packet: ByteArray, seq: Int) {
        packet[0] = MAGIC[0]
        packet[1] = MAGIC[1]
        packet[2] = AUDIO
        packet[3] = seq.toByte()
        packet[4] = (seq shr 8).toByte()
    }

    /** Broadcasts DISCOVER on the Wi-Fi network and collects ANNOUNCE replies. Blocking. */
    fun discover(ctx: Context, port: Int, timeoutMs: Long = 1500): List<Pc> {
        val found = LinkedHashMap<String, Pc>()
        DatagramSocket().use { s ->
            wifiNetwork(ctx)?.bindSocket(s)
            s.broadcast = true
            s.soTimeout = 200
            val buf = ByteArray(256)
            val p = DatagramPacket(buf, buf.size)
            val end = SystemClock.elapsedRealtime() + timeoutMs
            var nextSend = 0L
            while (SystemClock.elapsedRealtime() < end) {
                if (SystemClock.elapsedRealtime() >= nextSend) {
                    broadcast(ctx, s, port) // repeated: one lost broadcast shouldn't hide the PC
                    nextSend = SystemClock.elapsedRealtime() + 500
                }
                try {
                    p.length = buf.size
                    s.receive(p)
                } catch (e: SocketTimeoutException) {
                    continue
                }
                val (type, name) = parse(buf, p.length) ?: continue
                if (type == ANNOUNCE) found[p.address.hostAddress ?: continue] = Pc(name, p.address)
            }
        }
        return found.values.toList()
    }

    /** Sends DISCOVER to the subnet broadcast address(es) and 255.255.255.255. */
    fun broadcast(ctx: Context, s: DatagramSocket, port: Int) {
        val req = msg(DISCOVER)
        for (addr in broadcastAddresses(ctx) + InetAddress.getByName("255.255.255.255")) {
            try {
                s.send(DatagramPacket(req, req.size, addr, port))
            } catch (e: IOException) {
                // try the next address
            }
        }
    }

    /**
     * The Wi-Fi (or Ethernet) network. Sockets get bound to it because Android routes
     * through mobile data when the Wi-Fi has no internet, and the PC is never there.
     */
    fun wifiNetwork(ctx: Context): Network? {
        val cm = ctx.getSystemService(ConnectivityManager::class.java)
        @Suppress("DEPRECATION")
        return cm.allNetworks.firstOrNull {
            val caps = cm.getNetworkCapabilities(it) ?: return@firstOrNull false
            caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI) ||
                caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)
        }
    }

    private fun broadcastAddresses(ctx: Context): List<InetAddress> {
        val net = wifiNetwork(ctx) ?: return emptyList()
        val links = ctx.getSystemService(ConnectivityManager::class.java).getLinkProperties(net)
        return links?.linkAddresses.orEmpty().mapNotNull { la ->
            val a = la.address as? Inet4Address ?: return@mapNotNull null
            val hostBits = if (la.prefixLength >= 32) 0 else -1 ushr la.prefixLength
            val ip = ByteBuffer.wrap(a.address).int or hostBits
            InetAddress.getByAddress(ByteBuffer.allocate(4).putInt(ip).array())
        }
    }

    private fun le32(v: Int) = ByteBuffer.allocate(4).order(ByteOrder.LITTLE_ENDIAN).putInt(v).array()

    private fun nameBytes(name: String) = name.toByteArray(Charsets.UTF_8).let { it.copyOf(minOf(it.size, 64)) }
}
