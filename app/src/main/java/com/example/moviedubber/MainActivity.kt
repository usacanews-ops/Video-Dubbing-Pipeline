package com.example.moviedubber

import android.annotation.SuppressLint
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.net.Uri
import android.os.Bundle
import android.widget.Toast
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.clickable
import androidx.compose.foundation.horizontalScroll
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import kotlinx.coroutines.*
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import okio.Buffer
import okio.BufferedSink
import okio.ForwardingSink
import okio.buffer
import okio.source
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import java.text.SimpleDateFormat
import java.util.*
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.TimeUnit
import java.util.regex.Pattern

data class FacebookPageAccount(
    val name: String,
    val id: String,
    val token: String
)

data class QueueItem(
    val id: Long = System.currentTimeMillis(),
    val videoUrl: String,
    val targetLang: String = "hi",
    val speed: Float = 1.0f
)

data class HistoryItem(
    val id: String,
    var title: String,
    val downloadUrl: String,
    val srtUrl: String,
    val timestamp: String,
    var sourceMetaText: String = "",
    var isUploaded: Boolean = false
)

class ProgressRequestBody(
    private val file: File,
    private val contentType: MediaType,
    private val onProgressUpdate: (percentage: Int, bytesWritten: Long, totalBytes: Long) -> Unit
) : RequestBody() {

    override fun contentType(): MediaType = contentType
    override fun contentLength(): Long = file.length()

    override fun writeTo(sink: BufferedSink) {
        val totalLength = contentLength()
        val countingSink = object : ForwardingSink(sink) {
            var bytesWritten = 0L
            override fun write(source: Buffer, byteCount: Long) {
                super.write(source, byteCount)
                bytesWritten += byteCount
                val progress = if (totalLength > 0) ((bytesWritten * 100) / totalLength).toInt() else 0
                onProgressUpdate(progress, bytesWritten, totalLength)
            }
        }
        val bufferedCountingSink = countingSink.buffer()
        file.source().use { source ->
            bufferedCountingSink.writeAll(source)
            bufferedCountingSink.flush()
        }
    }
}

object DubberQueueManager {
    val queue = ConcurrentLinkedQueue<QueueItem>()
    var isProcessing by mutableStateOf(false)
    var currentStatus by mutableStateOf("Ready")
    var detailedLogs by mutableStateOf("")
    var firstLinePreview by mutableStateOf("")
    var queueSize by mutableStateOf(0)
    var historyList = mutableStateListOf<HistoryItem>()
    var lastFailedItem by mutableStateOf<QueueItem?>(null)

    private val coroutineScope = CoroutineScope(Dispatchers.IO + SupervisorJob())

    fun loadHistory(context: Context) {
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val jsonStr = prefs.getString("history_json", "[]") ?: "[]"
        try {
            val arr = JSONArray(jsonStr)
            historyList.clear()
            for (i in 0 until arr.length()) {
                val obj = arr.getJSONObject(i)
                historyList.add(
                    HistoryItem(
                        id = obj.getString("id"),
                        title = obj.optString("title", "Dubbed Video"),
                        downloadUrl = obj.getString("downloadUrl"),
                        srtUrl = obj.optString("srtUrl", ""),
                        timestamp = obj.getString("timestamp"),
                        sourceMetaText = obj.optString("sourceMetaText", ""),
                        isUploaded = obj.optBoolean("isUploaded", false)
                    )
                )
            }
        } catch (e: Exception) {
            e.printStackTrace()
        }
    }

    fun saveHistory(context: Context) {
        val arr = JSONArray()
        for (h in historyList) {
            val obj = JSONObject().apply {
                put("id", h.id)
                put("title", h.title)
                put("downloadUrl", h.downloadUrl)
                put("srtUrl", h.srtUrl)
                put("timestamp", h.timestamp)
                put("sourceMetaText", h.sourceMetaText)
                put("isUploaded", h.isUploaded)
            }
            arr.put(obj)
        }
        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        prefs.edit().putString("history_json", arr.toString()).apply()
    }

    fun removeItem(context: Context, item: HistoryItem) {
        historyList.remove(item)
        saveHistory(context)
    }

    fun clearAll(context: Context) {
        historyList.clear()
        saveHistory(context)
    }

    fun addHistoryItem(context: Context, item: HistoryItem) {
        historyList.add(0, item)
        while (historyList.size > 10) {
            historyList.removeAt(historyList.size - 1)
        }
        saveHistory(context)
    }

    fun enqueue(item: QueueItem, context: Context) {
        queue.add(item)
        queueSize = queue.size
        processNext(context.applicationContext)
    }

    fun retryFailedItem(context: Context) {
        lastFailedItem?.let { failed ->
            lastFailedItem = null
            enqueue(failed, context)
        }
    }

    fun processNext(context: Context) {
        if (isProcessing || queue.isEmpty()) return

        val nextItem = queue.poll() ?: return
        queueSize = queue.size
        isProcessing = true

        val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
        val owner = prefs.getString("owner", "usacanews-ops") ?: ""
        val repo = prefs.getString("repo", "Video-Dubbing-Pipeline") ?: ""
        val token = prefs.getString("token", "") ?: ""

        coroutineScope.launch {
            executeCloudDubbingPipeline(
                context = context,
                owner = owner.trim(),
                repo = repo.trim(),
                token = token.trim(),
                videoUrl = nextItem.videoUrl.trim(),
                targetLang = nextItem.targetLang,
                speed = nextItem.speed,
                onStatusUpdate = { status, log, preview ->
                    currentStatus = status
                    detailedLogs = log
                    if (preview.isNotBlank()) {
                        val words = preview.trim().split(Regex("\\s+"))
                        firstLinePreview = if (words.size > 5) words.take(5).joinToString(" ") + "..." else preview
                    }
                },
                onComplete = { _, _ ->
                    isProcessing = false
                    lastFailedItem = null
                    currentStatus = "🎉 Dubbing complete! Ready to schedule or upload."
                    processNext(context)
                },
                onError = { err ->
                    isProcessing = false
                    lastFailedItem = nextItem
                    currentStatus = "❌ Pipeline error: $err"
                    processNext(context)
                }
            )
        }
    }
}

class MainActivity : ComponentActivity() {

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        DubberQueueManager.loadHistory(this)
        handleIncomingShare(intent)

        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    DubberLiveApp()
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        handleIncomingShare(intent)
    }

    private fun handleIncomingShare(intent: Intent?) {
        if (intent?.action == Intent.ACTION_SEND && intent.type == "text/plain") {
            val sharedText = intent.getStringExtra(Intent.EXTRA_TEXT) ?: ""
            val extractedUrl = extractUrl(sharedText)

            if (!extractedUrl.isNullOrBlank()) {
                val prefs = getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)
                val lang = prefs.getString("default_lang", "hi") ?: "hi"
                val speed = prefs.getFloat("default_speed", 1.0f)

                DubberQueueManager.enqueue(
                    QueueItem(videoUrl = extractedUrl, targetLang = lang, speed = speed),
                    this
                )
                Toast.makeText(this, "📥 Added to processing queue", Toast.LENGTH_SHORT).show()
            }
        }
    }

    private fun extractUrl(text: String): String? {
        val matcher = Pattern.compile("https?://[\\w\\-._~:/?#\\[\\]@!$&'()*+,;=%]+").matcher(text)
        return if (matcher.find()) matcher.group(0) else null
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DubberLiveApp() {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()
    val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)

    var cloudWorkspace by remember { mutableStateOf(prefs.getString("owner", "usacanews-ops") ?: "") }
    var cloudRepository by remember { mutableStateOf(prefs.getString("repo", "Video-Dubbing-Pipeline") ?: "") }
    var cloudServerKey by remember { mutableStateOf(prefs.getString("token", "") ?: "") }

    var fbPagesRaw by remember {
        mutableStateOf(prefs.getString("fb_pages_raw", "Primary Page|100085938472910|EAA...") ?: "")
    }

    var customTitle by remember { mutableStateOf(prefs.getString("custom_title", "") ?: "") }
    var customTags by remember {
        mutableStateOf(prefs.getString("custom_tags", "#fyp #moviejet #reels #hindidubbed #movieexplained") ?: "")
    }

    var videoUrl by remember { mutableStateOf("") }
    var selectedLanguage by remember { mutableStateOf("hi") }
    var showSettings by remember { mutableStateOf(cloudServerKey.isBlank()) }

    var fbUploadStage by remember { mutableStateOf("") }
    var fbUploadPercent by remember { mutableIntStateOf(0) }
    var isUploadingToFb by remember { mutableStateOf(false) }

    var activeMetadataDialog by remember { mutableStateOf<String?>(null) }

    val pageAccounts = remember(fbPagesRaw) {
        fbPagesRaw.lines().mapNotNull { line ->
            val parts = line.split("|")
            if (parts.size >= 3) {
                FacebookPageAccount(parts[0].trim(), parts[1].trim(), parts[2].trim())
            } else null
        }
    }

    var selectedPageAccount by remember(pageAccounts) {
        mutableStateOf(pageAccounts.firstOrNull())
    }

    activeMetadataDialog?.let { metaText ->
        AlertDialog(
            onDismissRequest = { activeMetadataDialog = null },
            title = { Text("ℹ️ Source Video Metadata", fontSize = 16.sp, fontWeight = FontWeight.Bold) },
            text = {
                Card(
                    modifier = Modifier.fillMaxWidth().height(260.dp),
                    colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
                ) {
                    Box(
                        modifier = Modifier
                            .fillMaxSize()
                            .padding(10.dp)
                            .verticalScroll(rememberScrollState())
                    ) {
                        SelectionContainer {
                            Text(
                                text = metaText,
                                fontSize = 12.sp,
                                lineHeight = 16.sp,
                                color = MaterialTheme.colorScheme.onSurfaceVariant
                            )
                        }
                    }
                }
            },
            confirmButton = {
                Button(onClick = {
                    val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    clipboard.setPrimaryClip(ClipData.newPlainText("Source Metadata", metaText))
                    Toast.makeText(context, "📋 All metadata copied!", Toast.LENGTH_SHORT).show()
                    activeMetadataDialog = null
                }) {
                    Text("📋 Copy All")
                }
            },
            dismissButton = {
                TextButton(onClick = { activeMetadataDialog = null }) {
                    Text("Close")
                }
            }
        )
    }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(16.dp)
            .verticalScroll(rememberScrollState()),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Column {
                Text("🎬 AI Movie Dubber", fontSize = 22.sp, fontWeight = FontWeight.Bold)
                if (DubberQueueManager.queueSize > 0 || DubberQueueManager.isProcessing) {
                    Text(
                        "Queue: ${DubberQueueManager.queueSize} waiting | Active: ${if (DubberQueueManager.isProcessing) "1" else "0"}",
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.primary,
                        fontWeight = FontWeight.SemiBold
                    )
                }
            }
            TextButton(onClick = { showSettings = !showSettings }) {
                Text(if (showSettings) "Close Config" else "⚙️ Config")
            }
        }

        if (showSettings) {
            Card(
                modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
            ) {
                Column(modifier = Modifier.padding(14.dp)) {
                    Text("Cloud Engine Settings", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = cloudWorkspace,
                        onValueChange = { cloudWorkspace = it; prefs.edit().putString("owner", it).apply() },
                        label = { Text("Workspace / User") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = cloudRepository,
                        onValueChange = { cloudRepository = it; prefs.edit().putString("repo", it).apply() },
                        label = { Text("Pipeline Service Name") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = cloudServerKey,
                        onValueChange = { cloudServerKey = it; prefs.edit().putString("token", it).apply() },
                        label = { Text("Pipeline Server Access Key") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )

                    Spacer(modifier = Modifier.height(10.dp))
                    Text("Facebook Pages (1 per line)", fontWeight = FontWeight.Bold, fontSize = 14.sp)
                    Text("Format: PageName|PageID|PageToken", fontSize = 11.sp, color = Color.Gray)
                    Spacer(modifier = Modifier.height(4.dp))
                    OutlinedTextField(
                        value = fbPagesRaw,
                        onValueChange = {
                            fbPagesRaw = it
                            prefs.edit().putString("fb_pages_raw", it).apply()
                        },
                        modifier = Modifier.fillMaxWidth(),
                        minLines = 3
                    )
                }
            }
        }

        Spacer(modifier = Modifier.height(6.dp))

        OutlinedTextField(
            value = customTitle,
            onValueChange = {
                customTitle = it
                prefs.edit().putString("custom_title", it).apply()
            },
            label = { Text("Custom Reel Title") },
            placeholder = { Text("Movie Explained in Hindi") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true,
            trailingIcon = {
                if (customTitle.isNotEmpty()) {
                    IconButton(onClick = {
                        customTitle = ""
                        prefs.edit().putString("custom_title", "").apply()
                    }) {
                        Text("✕", fontSize = 14.sp, fontWeight = FontWeight.Bold)
                    }
                }
            }
        )

        Spacer(modifier = Modifier.height(6.dp))

        OutlinedTextField(
            value = customTags,
            onValueChange = {
                customTags = it
                prefs.edit().putString("custom_tags", it).apply()
            },
            label = { Text("Custom #Tags") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Spacer(modifier = Modifier.height(8.dp))

        OutlinedTextField(
            value = videoUrl,
            onValueChange = { videoUrl = it },
            label = { Text("Paste Video Link (FB / YouTube)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Spacer(modifier = Modifier.height(8.dp))

        Text("Target Dubbing Language", modifier = Modifier.align(Alignment.Start), fontSize = 13.sp)
        Spacer(modifier = Modifier.height(4.dp))
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .horizontalScroll(rememberScrollState()),
            horizontalArrangement = Arrangement.spacedBy(8.dp)
        ) {
            listOf(
                "hi" to "Hindi",
                "en" to "English (Re-voice)",
                "es" to "Spanish",
                "fr" to "French",
                "de" to "German",
                "it" to "Italian",
                "pt" to "Portuguese",
                "ja" to "Japanese"
            ).forEach { (code, name) ->
                FilterChip(
                    selected = selectedLanguage == code,
                    onClick = {
                        selectedLanguage = code
                        prefs.edit().putString("default_lang", code).apply()
                    },
                    label = { Text(name, fontSize = 12.sp, maxLines = 1) }
                )
            }
        }

        Spacer(modifier = Modifier.height(10.dp))

        Button(
            onClick = {
                if (videoUrl.isNotBlank()) {
                    DubberQueueManager.enqueue(
                        QueueItem(videoUrl = videoUrl.trim(), targetLang = selectedLanguage),
                        context
                    )
                    videoUrl = ""
                }
            },
            modifier = Modifier.fillMaxWidth().height(48.dp),
            enabled = videoUrl.isNotBlank() && cloudServerKey.isNotBlank()
        ) {
            Text("➕ Add to Processing Queue", fontSize = 15.sp)
        }

        Spacer(modifier = Modifier.height(10.dp))

        if (isUploadingToFb) {
            Card(
                modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.primaryContainer)
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Text(
                        text = "🚀 $fbUploadStage",
                        fontSize = 13.sp,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.onPrimaryContainer
                    )
                    Spacer(modifier = Modifier.height(6.dp))
                    LinearProgressIndicator(
                        progress = fbUploadPercent / 100f,
                        modifier = Modifier.fillMaxWidth().height(8.dp),
                    )
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "$fbUploadPercent%",
                        fontSize = 11.sp,
                        modifier = Modifier.align(Alignment.End),
                        color = MaterialTheme.colorScheme.onPrimaryContainer
                    )
                }
            }
        }

        Card(
            modifier = Modifier.fillMaxWidth().padding(vertical = 6.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.secondaryContainer)
        ) {
            Column(modifier = Modifier.padding(12.dp)) {
                Row(verticalAlignment = Alignment.CenterVertically) {
                    if (DubberQueueManager.isProcessing) {
                        CircularProgressIndicator(
                            modifier = Modifier.size(16.dp),
                            strokeWidth = 2.dp,
                            color = MaterialTheme.colorScheme.primary
                        )
                        Spacer(modifier = Modifier.width(8.dp))
                    }
                    Text(
                        text = DubberQueueManager.currentStatus,
                        fontSize = 13.sp,
                        fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.onSecondaryContainer
                    )
                }

                if (!DubberQueueManager.isProcessing && DubberQueueManager.lastFailedItem != null) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Button(
                        onClick = { DubberQueueManager.retryFailedItem(context) },
                        colors = ButtonDefaults.buttonColors(containerColor = MaterialTheme.colorScheme.error),
                        modifier = Modifier.fillMaxWidth().height(36.dp),
                        contentPadding = PaddingValues(horizontal = 8.dp)
                    ) {
                        Text("🔄 Retry Connection Now", fontSize = 12.sp, color = Color.White)
                    }
                }

                if (DubberQueueManager.firstLinePreview.isNotBlank()) {
                    Spacer(modifier = Modifier.height(4.dp))
                    Text(
                        text = "🗣️ Spoken: \"${DubberQueueManager.firstLinePreview}\"",
                        fontSize = 11.sp,
                        color = MaterialTheme.colorScheme.onSecondaryContainer.copy(alpha = 0.8f)
                    )
                }

                if (DubberQueueManager.detailedLogs.isNotBlank()) {
                    Spacer(modifier = Modifier.height(6.dp))
                    Box(
                        modifier = Modifier
                            .fillMaxWidth()
                            .heightIn(max = 120.dp)
                            .verticalScroll(rememberScrollState())
                    ) {
                        Text(
                            text = DubberQueueManager.detailedLogs,
                            fontSize = 10.sp,
                            fontFamily = FontFamily.Monospace,
                            color = MaterialTheme.colorScheme.onSecondaryContainer
                        )
                    }
                }
            }
        }

        if (pageAccounts.isNotEmpty()) {
            Spacer(modifier = Modifier.height(8.dp))
            Text("Publish Destination Page", modifier = Modifier.align(Alignment.Start), fontSize = 13.sp)
            Spacer(modifier = Modifier.height(4.dp))
            Row(
                modifier = Modifier
                    .fillMaxWidth()
                    .horizontalScroll(rememberScrollState()),
                horizontalArrangement = Arrangement.spacedBy(8.dp)
            ) {
                pageAccounts.forEach { acc ->
                    FilterChip(
                        selected = selectedPageAccount?.id == acc.id,
                        onClick = { selectedPageAccount = acc },
                        label = { Text(acc.name, fontSize = 12.sp) }
                    )
                }
            }
        }

        Spacer(modifier = Modifier.height(12.dp))

        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("Recent Dubs", fontSize = 16.sp, fontWeight = FontWeight.Bold)
            if (DubberQueueManager.historyList.isNotEmpty()) {
                TextButton(onClick = { DubberQueueManager.clearAll(context) }) {
                    Text("Clear All", fontSize = 12.sp, color = MaterialTheme.colorScheme.error)
                }
            }
        }

        DubberQueueManager.historyList.forEach { item ->
            Card(
                modifier = Modifier
                    .fillMaxWidth()
                    .padding(vertical = 4.dp),
                shape = RoundedCornerShape(8.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surface)
            ) {
                Column(modifier = Modifier.padding(12.dp)) {
                    Text(
                        text = item.title,
                        fontWeight = FontWeight.Bold,
                        fontSize = 14.sp,
                        maxLines = 2,
                        overflow = TextOverflow.Ellipsis,
                        modifier = Modifier.clickable {
                            customTitle = item.title
                            prefs.edit().putString("custom_title", item.title).apply()
                            Toast.makeText(context, "Filled title input", Toast.LENGTH_SHORT).show()
                        }
                    )
                    Text(
                        text = item.timestamp,
                        fontSize = 10.sp,
                        color = Color.Gray
                    )

                    Spacer(modifier = Modifier.height(8.dp))

                    Row(
                        modifier = Modifier.fillMaxWidth(),
                        horizontalArrangement = Arrangement.spacedBy(6.dp),
                        verticalAlignment = Alignment.CenterVertically
                    ) {
                        if (item.sourceMetaText.isNotBlank()) {
                            OutlinedButton(
                                onClick = { activeMetadataDialog = item.sourceMetaText },
                                modifier = Modifier.height(34.dp),
                                contentPadding = PaddingValues(horizontal = 8.dp)
                            ) {
                                Text("ℹ️ Meta", fontSize = 11.sp)
                            }
                        }

                        OutlinedButton(
                            onClick = {
                                val browserIntent = Intent(Intent.ACTION_VIEW, Uri.parse(item.downloadUrl))
                                context.startActivity(browserIntent)
                            },
                            modifier = Modifier.height(34.dp),
                            contentPadding = PaddingValues(horizontal = 8.dp)
                        ) {
                            Text("⬇️ Video", fontSize = 11.sp)
                        }

                        Button(
                            onClick = {
                                selectedPageAccount?.let { page ->
                                    val fullCaption = buildString {
                                        append(customTitle.ifBlank { item.title })
                                        if (customTags.isNotBlank()) {
                                            append("\n\n")
                                            append(customTags)
                                        }
                                    }
                                    isUploadingToFb = true
                                    coroutineScope.launch {
                                        uploadVideoToFacebookReel(
                                            context = context,
                                            pageAccount = page,
                                            videoUrl = item.downloadUrl,
                                            caption = fullCaption,
                                            onProgress = { stage, percent ->
                                                fbUploadStage = stage
                                                fbUploadPercent = percent
                                            },
                                            onSuccess = {
                                                isUploadingToFb = false
                                                item.isUploaded = true
                                                DubberQueueManager.saveHistory(context)
                                                
                                                customTitle = ""
                                                prefs.edit().putString("custom_title", "").apply()
                                                
                                                Toast.makeText(context, "✅ Published to ${page.name}!", Toast.LENGTH_LONG).show()
                                            },
                                            onError = { err ->
                                                isUploadingToFb = false
                                                Toast.makeText(context, "Upload Failed: $err", Toast.LENGTH_LONG).show()
                                            }
                                        )
                                    }
                                } ?: run {
                                    Toast.makeText(context, "Select a Facebook Page first", Toast.LENGTH_SHORT).show()
                                }
                            },
                            enabled = !isUploadingToFb && selectedPageAccount != null,
                            modifier = Modifier.height(34.dp),
                            contentPadding = PaddingValues(horizontal = 10.dp)
                        ) {
                            Text(if (item.isUploaded) "🔁 Re-Upload" else "🚀 Publish Reel", fontSize = 11.sp)
                        }

                        Spacer(modifier = Modifier.weight(1f))

                        IconButton(
                            onClick = { DubberQueueManager.removeItem(context, item) },
                            modifier = Modifier.size(32.dp)
                        ) {
                            Text("🗑️", fontSize = 12.sp)
                        }
                    }
                }
            }
        }
    }
}

// -------------------------------------------------------------------------------------
// Network State Verification
// -------------------------------------------------------------------------------------
@SuppressLint("MissingPermission")
fun isNetworkAvailable(context: Context): Boolean {
    val cm = context.getSystemService(Context.CONNECTIVITY_SERVICE) as? ConnectivityManager ?: return true
    val activeNet = cm.activeNetwork ?: return false
    val caps = cm.getNetworkCapabilities(activeNet) ?: return false
    return caps.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
}

// -------------------------------------------------------------------------------------
// Robust HTTP Request with Auto Reconnect
// -------------------------------------------------------------------------------------
suspend fun executeWithRetry(
    context: Context,
    client: OkHttpClient,
    request: Request,
    maxRetries: Int = 12,
    onWaitingNetwork: (msg: String) -> Unit = {}
): Response {
    var attempt = 0
    var delayMs = 2000L

    while (true) {
        attempt++
        try {
            while (!isNetworkAvailable(context)) {
                onWaitingNetwork("📡 Waiting for network to return...")
                delay(3000)
            }
            return withContext(Dispatchers.IO) {
                client.newCall(request).execute()
            }
        } catch (e: IOException) {
            if (attempt >= maxRetries) throw e
            onWaitingNetwork("⚠️ Connection interrupted. Reconnecting ($attempt/$maxRetries)...")
            delay(delayMs)
            delayMs = (delayMs * 1.5).toLong().coerceAtMost(15000L)
        }
    }
}

// -------------------------------------------------------------------------------------
// Cloud Dubbing Pipeline Execution (GitHub Actions / Cloud Dispatcher)
// -------------------------------------------------------------------------------------
suspend fun executeCloudDubbingPipeline(
    context: Context,
    owner: String,
    repo: String,
    token: String,
    videoUrl: String,
    targetLang: String,
    speed: Float,
    onStatusUpdate: (status: String, log: String, preview: String) -> Unit,
    onComplete: (downloadUrl: String, title: String) -> Unit,
    onError: (error: String) -> Unit
) = withContext(Dispatchers.IO) {
    val client = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    try {
        onStatusUpdate("Checking existing workflows...", "", "")

        // 0. Cache existing run IDs to ensure we pick up the newly generated one
        val runsUrl = "https://api.github.com/repos/$owner/$repo/actions/runs?per_page=5"
        val existingRunIds = mutableSetOf<Long>()

        try {
            val listBeforeReq = Request.Builder()
                .url(runsUrl)
                .addHeader("Authorization", "Bearer $token")
                .addHeader("Accept", "application/vnd.github.v3+json")
                .build()

            executeWithRetry(context, client, listBeforeReq, maxRetries = 2).use { res ->
                if (res.isSuccessful) {
                    val bodyStr = res.body?.string() ?: "{}"
                    val runs = JSONObject(bodyStr).optJSONArray("workflow_runs")
                    if (runs != null) {
                        for (i in 0 until runs.length()) {
                            existingRunIds.add(runs.getJSONObject(i).optLong("id"))
                        }
                    }
                }
            }
        } catch (ignored: Exception) {}

        onStatusUpdate("Triggering remote cloud worker...", "", "")

        // 1. Dispatch Repository Workflow
        val dispatchUrl = "https://api.github.com/repos/$owner/$repo/dispatches"
        val payload = JSONObject().apply {
            put("event_type", "run_dubber")
            put("client_payload", JSONObject().apply {
                put("video_url", videoUrl)
                put("target_lang", targetLang)
                put("speed", speed.toString())
            })
        }

        val dispatchReq = Request.Builder()
            .url(dispatchUrl)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Accept", "application/vnd.github.v3+json")
            .post(payload.toString().toRequestBody("application/json".toMediaType()))
            .build()

        executeWithRetry(context, client, dispatchReq) { retryMsg ->
            onStatusUpdate(retryMsg, "", "")
        }.use { res ->
            if (!res.isSuccessful && res.code != 204) {
                val errorDetails = res.body?.string() ?: res.message
                onError("Failed to trigger pipeline (${res.code}): $errorDetails")
                return@withContext
            }
        }

        // 2. Poll for the newly started workflow run ID (up to 25 attempts / ~75 seconds)
        var runId: Long? = null

        for (attempt in 1..25) {
            delay(3000)
            onStatusUpdate("Registering remote worker ($attempt/25)...", "", "")

            val listReq = Request.Builder()
                .url(runsUrl)
                .addHeader("Authorization", "Bearer $token")
                .addHeader("Accept", "application/vnd.github.v3+json")
                .build()

            try {
                executeWithRetry(context, client, listReq, maxRetries = 3).use { res ->
                    if (res.isSuccessful) {
                        val bodyStr = res.body?.string() ?: "{}"
                        val runs = JSONObject(bodyStr).optJSONArray("workflow_runs")
                        if (runs != null && runs.length() > 0) {
                            for (i in 0 until runs.length()) {
                                val currentId = runs.getJSONObject(i).optLong("id")
                                if (!existingRunIds.contains(currentId)) {
                                    runId = currentId
                                    break
                                }
                            }
                            if (runId == null && existingRunIds.isEmpty()) {
                                runId = runs.getJSONObject(0).optLong("id")
                            }
                        }
                    }
                }
            } catch (ignored: Exception) {}

            if (runId != null) break
        }

        if (runId == null) {
            onError("Workflow run could not be registered. Ensure your workflow YAML contains 'repository_dispatch: types: [run_dubber]' on the default branch.")
            return@withContext
        }

        // 3. Poll Workflow Run Status
        val checkUrl = "https://api.github.com/repos/$owner/$repo/actions/runs/$runId"
        var isFinished = false
        var currentTitle = "Dubbed Video"
        var sourceMetaText = ""

        while (!isFinished) {
            delay(5000)
            val checkReq = Request.Builder()
                .url(checkUrl)
                .addHeader("Authorization", "Bearer $token")
                .addHeader("Accept", "application/vnd.github.v3+json")
                .build()

            try {
                executeWithRetry(context, client, checkReq, maxRetries = 5) { retryMsg ->
                    onStatusUpdate(retryMsg, "", "")
                }.use { res ->
                    if (res.isSuccessful) {
                        val runObj = JSONObject(res.body?.string() ?: "{}")
                        val status = runObj.optString("status")
                        val conclusion = runObj.optString("conclusion")

                        onStatusUpdate("Engine state: $status...", "", "")

                        if (status == "completed") {
                            isFinished = true
                            if (conclusion != "success") {
                                onError("Pipeline execution ended with status: $conclusion")
                                return@withContext
                            }
                        }
                    }
                }
            } catch (e: Exception) {
                onStatusUpdate("Signal dropped, maintaining pipeline sync...", "", "")
            }
        }

        // 4. Retrieve Published Output Artifacts
        val artifactsUrl = "https://api.github.com/repos/$owner/$repo/actions/runs/$runId/artifacts"
        var downloadUrl = ""

        val artReq = Request.Builder()
            .url(artifactsUrl)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Accept", "application/vnd.github.v3+json")
            .build()

        executeWithRetry(context, client, artReq).use { res ->
            if (res.isSuccessful) {
                val artObj = JSONObject(res.body?.string() ?: "{}")
                val arts = artObj.optJSONArray("artifacts")
                if (arts != null && arts.length() > 0) {
                    for (i in 0 until arts.length()) {
                        val item = arts.getJSONObject(i)
                        if (item.optString("name").contains("video", ignoreCase = true)) {
                            downloadUrl = item.optString("archive_download_url")
                        }
                    }
                }
            }
        }

        val timestamp = SimpleDateFormat("dd MMM, hh:mm a", Locale.getDefault()).format(Date())
        val historyItem = HistoryItem(
            id = UUID.randomUUID().toString(),
            title = currentTitle,
            downloadUrl = downloadUrl.ifBlank { "https://github.com/$owner/$repo/actions/runs/$runId" },
            srtUrl = "",
            timestamp = timestamp,
            sourceMetaText = sourceMetaText
        )

        withContext(Dispatchers.Main) {
            DubberQueueManager.addHistoryItem(context, historyItem)
        }

        onComplete(historyItem.downloadUrl, currentTitle)

    } catch (e: Exception) {
        onError(e.message ?: "Unknown cloud execution error")
    }
}

// -------------------------------------------------------------------------------------
// Facebook Graph API Video Reels Chunked/Direct Upload
// -------------------------------------------------------------------------------------
suspend fun uploadVideoToFacebookReel(
    context: Context,
    pageAccount: FacebookPageAccount,
    videoUrl: String,
    caption: String,
    onProgress: (stage: String, percent: Int) -> Unit,
    onSuccess: () -> Unit,
    onError: (error: String) -> Unit
) = withContext(Dispatchers.IO) {
    val client = OkHttpClient.Builder()
        .connectTimeout(60, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    var tempFile: File? = null

    try {
        onProgress("Buffering video stream...", 10)

        tempFile = File(context.cacheDir, "fb_upload_${System.currentTimeMillis()}.mp4")
        val getReq = Request.Builder().url(videoUrl).build()

        executeWithRetry(context, client, getReq).use { res ->
            if (!res.isSuccessful) throw Exception("Failed to fetch processed video: HTTP ${res.code}")
            val body = res.body ?: throw Exception("Empty video response body")

            val totalBytes = body.contentLength()
            var bytesCopied = 0L

            body.byteStream().use { input ->
                FileOutputStream(tempFile).use { output ->
                    val buffer = ByteArray(8 * 1024)
                    var bytes = input.read(buffer)
                    while (bytes >= 0) {
                        output.write(buffer, 0, bytes)
                        bytesCopied += bytes
                        val percent = if (totalBytes > 0) ((bytesCopied * 40) / totalBytes).toInt() + 10 else 25
                        onProgress("Buffering video stream...", percent.coerceAtMost(49))
                        bytes = input.read(buffer)
                    }
                }
            }
        }

        onProgress("Initializing Reel upload session...", 50)

        val initUrl = "https://graph.facebook.com/v19.0/${pageAccount.id}/video_reels"
        val initPayload = FormBody.Builder()
            .add("upload_phase", "start")
            .add("access_token", pageAccount.token)
            .build()

        val initReq = Request.Builder().url(initUrl).post(initPayload).build()
        val (videoId, uploadUrl) = executeWithRetry(context, client, initReq).use { res ->
            val json = JSONObject(res.body?.string() ?: "{}")
            if (!res.isSuccessful) {
                throw Exception("Init failed: ${json.optJSONObject("error")?.optString("message") ?: res.message}")
            }
            Pair(json.getString("video_id"), json.getString("upload_url"))
        }

        onProgress("Uploading video binary...", 55)

        val progressBody = ProgressRequestBody(
            file = tempFile,
            contentType = "application/octet-stream".toMediaType(),
            onProgressUpdate = { pct, _, _ ->
                val overall = 55 + ((pct * 35) / 100)
                onProgress("Uploading video ($pct%)...", overall.coerceAtMost(90))
            }
        )

        val uploadBinaryReq = Request.Builder()
            .url(uploadUrl)
            .addHeader("Authorization", "OAuth ${pageAccount.token}")
            .addHeader("offset", "0")
            .addHeader("file_size", tempFile.length().toString())
            .post(progressBody)
            .build()

        executeWithRetry(context, client, uploadBinaryReq).use { res ->
            if (!res.isSuccessful) {
                throw Exception("Upload binary transfer failed: HTTP ${res.code} ${res.body?.string()}")
            }
        }

        onProgress("Publishing Reel metadata...", 95)

        val finishPayload = FormBody.Builder()
            .add("upload_phase", "finish")
            .add("access_token", pageAccount.token)
            .add("video_id", videoId)
            .add("video_state", "PUBLISHED")
            .add("description", caption)
            .build()

        val finishReq = Request.Builder().url(initUrl).post(finishPayload).build()
        executeWithRetry(context, client, finishReq).use { res ->
            val json = JSONObject(res.body?.string() ?: "{}")
            if (!res.isSuccessful) {
                throw Exception("Publishing failed: ${json.optJSONObject("error")?.optString("message") ?: res.message}")
            }
        }

        onProgress("Reel published successfully!", 100)
        withContext(Dispatchers.Main) {
            onSuccess()
        }

    } catch (e: Exception) {
        withContext(Dispatchers.Main) {
            onError(e.message ?: "Facebook Reel upload encounter an error")
        }
    } finally {
        tempFile?.let {
            if (it.exists()) it.delete()
        }
    }
}
