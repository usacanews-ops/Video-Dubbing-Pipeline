package com.example.moviedubber

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.core.content.FileProvider
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import okhttp3.*
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.*
import java.util.zip.ZipInputStream

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContent {
            MaterialTheme {
                Surface(modifier = Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.background) {
                    DubberLiveApp()
                }
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DubberLiveApp() {
    val context = LocalContext.current
    val coroutineScope = rememberCoroutineScope()

    val prefs = context.getSharedPreferences("DubberPrefs", Context.MODE_PRIVATE)

    var githubOwner by remember { mutableStateOf(prefs.getString("owner", "") ?: "") }
    var githubRepo by remember { mutableStateOf(prefs.getString("repo", "") ?: "") }
    var githubToken by remember { mutableStateOf(prefs.getString("token", "") ?: "") }

    var videoUrl by remember { mutableStateOf("") }
    var selectedLanguage by remember { mutableStateOf("hi") }
    var videoSpeed by remember { mutableStateOf(1.0f) }

    var isProcessing by remember { mutableStateOf(false) }
    var currentStatus by remember { mutableStateOf("Ready to dub") }
    var detailedLogs by remember { mutableStateOf("") }
    var downloadedVideoFile by remember { mutableStateOf<File?>(null) }
    var showSettings by remember { mutableStateOf(githubToken.isBlank()) }

    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(20.dp)
            .verticalScroll(rememberScrollState()),
        horizontalAlignment = Alignment.CenterHorizontally
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically
        ) {
            Text("🎬 AI Movie Dubber", fontSize = 24.sp, fontWeight = FontWeight.Bold)
            TextButton(onClick = { showSettings = !showSettings }) {
                Text(if (showSettings) "Hide Config" else "⚙️ Config")
            }
        }

        if (showSettings) {
            Card(
                modifier = Modifier.fillMaxWidth().padding(vertical = 8.dp),
                colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
            ) {
                Column(modifier = Modifier.padding(16.dp)) {
                    Text("GitHub Setup (Runs the Free Cloud AI)", fontWeight = FontWeight.Bold)
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = githubOwner,
                        onValueChange = { githubOwner = it; prefs.edit().putString("owner", it).apply() },
                        label = { Text("GitHub Username / Owner") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = githubRepo,
                        onValueChange = { githubRepo = it; prefs.edit().putString("repo", it).apply() },
                        label = { Text("Repository Name") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                    Spacer(modifier = Modifier.height(8.dp))
                    OutlinedTextField(
                        value = githubToken,
                        onValueChange = { githubToken = it; prefs.edit().putString("token", it).apply() },
                        label = { Text("GitHub Personal Access Token (PAT)") },
                        modifier = Modifier.fillMaxWidth(),
                        singleLine = true
                    )
                }
            }
        }

        Spacer(modifier = Modifier.height(16.dp))

        OutlinedTextField(
            value = videoUrl,
            onValueChange = { videoUrl = it },
            label = { Text("Video URL (Facebook / YouTube)") },
            modifier = Modifier.fillMaxWidth(),
            singleLine = true
        )

        Spacer(modifier = Modifier.height(16.dp))

        Text("Target Language: ${if (selectedLanguage == "hi") "Hindi (hi)" else selectedLanguage}", modifier = Modifier.align(Alignment.Start))
        Row(modifier = Modifier.fillMaxWidth(), horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            listOf("hi" to "Hindi", "es" to "Spanish", "fr" to "French", "de" to "German").forEach { (code, name) ->
                FilterChip(
                    selected = selectedLanguage == code,
                    onClick = { selectedLanguage = code },
                    label = { Text(name) }
                )
            }
        }

        Spacer(modifier = Modifier.height(16.dp))

        Text("Speed Factor: ${String.format("%.2fx", videoSpeed)}", modifier = Modifier.align(Alignment.Start))
        Slider(
            value = videoSpeed,
            onValueChange = { videoSpeed = it },
            valueRange = 0.75f..1.5f,
            steps = 5,
            modifier = Modifier.fillMaxWidth()
        )

        Spacer(modifier = Modifier.height(24.dp))

        Button(
            onClick = {
                if (videoUrl.isNotBlank() && githubToken.isNotBlank()) {
                    isProcessing = true
                    downloadedVideoFile = null
                    coroutineScope.launch {
                        executeCloudDubbingPipeline(
                            context = context,
                            owner = githubOwner.trim(),
                            repo = githubRepo.trim(),
                            token = githubToken.trim(),
                            videoUrl = videoUrl.trim(),
                            targetLang = selectedLanguage,
                            speed = videoSpeed,
                            onStatusUpdate = { status, log ->
                                currentStatus = status
                                detailedLogs = log
                            },
                            onComplete = { file ->
                                isProcessing = false
                                downloadedVideoFile = file
                                currentStatus = "🎉 Video Ready & Downloaded!"
                            },
                            onError = { err ->
                                isProcessing = false
                                currentStatus = "❌ Error: $err"
                            }
                        )
                    }
                }
            },
            modifier = Modifier.fillMaxWidth().height(54.dp),
            enabled = !isProcessing && videoUrl.isNotBlank() && githubToken.isNotBlank()
        ) {
            Text(if (isProcessing) "Dubbing in Progress..." else "🚀 Start Dubbing Video", fontSize = 16.sp)
        }

        Spacer(modifier = Modifier.height(24.dp))

        if (isProcessing) {
            LinearProgressIndicator(modifier = Modifier.fillMaxWidth())
            Spacer(modifier = Modifier.height(12.dp))
        }

        // Live status & log console
        Card(
            modifier = Modifier.fillMaxWidth(),
            shape = RoundedCornerShape(8.dp),
            colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant)
        ) {
            Column(modifier = Modifier.padding(16.dp)) {
                Text(currentStatus, fontWeight = FontWeight.Bold, fontSize = 16.sp)
                if (detailedLogs.isNotBlank()) {
                    Spacer(modifier = Modifier.height(8.dp))
                    Text(
                        detailedLogs,
                        fontSize = 12.sp,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        fontFamily = androidx.compose.ui.text.font.FontFamily.Monospace
                    )
                }
            }
        }

        // Action button to play video directly
        downloadedVideoFile?.let { videoFile ->
            Spacer(modifier = Modifier.height(20.dp))
            Button(
                onClick = {
                    val uri: Uri = FileProvider.getUriForFile(context, "${context.packageName}.provider", videoFile)
                    val intent = Intent(Intent.ACTION_VIEW).apply {
                        setDataAndType(uri, "video/mp4")
                        addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                    }
                    context.startActivity(Intent.createChooser(intent, "Play Dubbed Video"))
                },
                modifier = Modifier.fillMaxWidth().height(54.dp),
                colors = ButtonDefaults.buttonColors(containerColor = Color(0xFF2E7D32))
            ) {
                Text("▶️ Play Dubbed Video in App / Gallery", fontSize = 16.sp, color = Color.White)
            }
        }
    }
}

// =========================================================================
// 🚀 GitHub REST API Orchestrator (Trigger, Poll, and Download)
// =========================================================================
suspend fun executeCloudDubbingPipeline(
    context: Context,
    owner: String,
    repo: String,
    token: String,
    videoUrl: String,
    targetLang: String,
    speed: Float,
    onStatusUpdate: (String, String) -> Unit,
    onComplete: (File) -> Unit,
    onError: (String) -> Unit
) = withContext(Dispatchers.IO) {
    val client = OkHttpClient()
    val authHeader = "Bearer $token"

    try {
        onStatusUpdate("⚡ Triggering Cloud Dubbing...", "Contacting GitHub Actions...")

        // 1. Dispatch Workflow
        val dispatchUrl = "https://api.github.com/repos/$owner/$repo/actions/workflows/dub_video.yml/dispatches"
        val payload = JSONObject().apply {
            put("ref", "main")
            put("inputs", JSONObject().apply {
                put("video_url", videoUrl)
                put("target_language", targetLang)
                put("video_speed", speed.toString())
            })
        }

        val request = Request.Builder()
            .url(dispatchUrl)
            .addHeader("Authorization", authHeader)
            .addHeader("Accept", "application/vnd.github+json")
            .post(payload.toString().toRequestBody("application/json".toMediaType()))
            .build()

        val response = client.newCall(request).execute()
        if (!response.isSuccessful && response.code != 204) {
            onError("Failed to start: HTTP ${response.code} (Check Token/Repo settings)")
            return@withContext
        }

        onStatusUpdate("⏳ Queued on GitHub...", "Waiting for cloud runner to start...")
        delay(6000)

        // 2. Poll for the Workflow Run ID
        var runId: Long? = null
        for (i in 1..10) {
            val runsUrl = "https://api.github.com/repos/$owner/$repo/actions/runs?event=workflow_dispatch&per_page=1"
            val runsReq = Request.Builder().url(runsUrl).addHeader("Authorization", authHeader).build()
            val runsRes = client.newCall(runsReq).execute()
            if (runsRes.isSuccessful) {
                val json = JSONObject(runsRes.body?.string() ?: "")
                val runsArray = json.optJSONArray("workflow_runs")
                if (runsArray != null && runsArray.length() > 0) {
                    runId = runsArray.getJSONObject(0).getLong("id")
                    break
                }
            }
            delay(3000)
        }

        if (runId == null) {
            onError("Could not detect started run on GitHub.")
            return@withContext
        }

        // 3. Poll Jobs and Steps for Live Status Updates
        var isDone = false
        var runConclusion = ""
        while (!isDone) {
            delay(3000)
            val jobUrl = "https://api.github.com/repos/$owner/$repo/actions/runs/$runId/jobs"
            val jobReq = Request.Builder().url(jobUrl).addHeader("Authorization", authHeader).build()
            val jobRes = client.newCall(jobReq).execute()

            if (jobRes.isSuccessful) {
                val jobJson = JSONObject(jobRes.body?.string() ?: "")
                val jobs = jobJson.optJSONArray("jobs")
                if (jobs != null && jobs.length() > 0) {
                    val job = jobs.getJSONObject(0)
                    val status = job.optString("status") // queued, in_progress, completed
                    val conclusion = job.optString("conclusion")
                    val steps = job.optJSONArray("steps")

                    var activeStepName = "Setting up cloud runner..."
                    if (steps != null) {
                        for (j in 0 until steps.length()) {
                            val step = steps.getJSONObject(j)
                            if (step.optString("status") == "in_progress") {
                                activeStepName = step.optString("name")
                                break
                            }
                        }
                    }

                    onStatusUpdate("⚙️ $activeStepName", "Status: $status | Run ID: $runId")

                    if (status == "completed") {
                        isDone = true
                        runConclusion = conclusion
                    }
                }
            }
        }

        if (runConclusion != "success") {
            onError("Pipeline finished with error: $runConclusion. Check video URL.")
            return@withContext
        }

        // 4. Download Resulting Video Artifact
        onStatusUpdate("📥 Downloading MP4 to Phone...", "Retrieving video artifact from cloud...")
        val artifactsUrl = "https://api.github.com/repos/$owner/$repo/actions/runs/$runId/artifacts"
        val artReq = Request.Builder().url(artifactsUrl).addHeader("Authorization", authHeader).build()
        val artRes = client.newCall(artReq).execute()

        var downloadUrl: String? = null
        if (artRes.isSuccessful) {
            val artJson = JSONObject(artRes.body?.string() ?: "")
            val arts = artJson.optJSONArray("artifacts")
            if (arts != null && arts.length() > 0) {
                downloadUrl = arts.getJSONObject(0).getString("archive_download_url")
            }
        }

        if (downloadUrl == null) {
            onError("Artifact not found in completed run.")
            return@withContext
        }

        // Download the ZIP file
        val dlReq = Request.Builder().url(downloadUrl).addHeader("Authorization", authHeader).build()
        val dlRes = client.newCall(dlReq).execute()

        val outputDir = File(context.getExternalFilesDir(null), "Movies").apply { mkdirs() }
        val finalMp4 = File(outputDir, "dubbed_explainer_${System.currentTimeMillis()}.mp4")

        // Unzip artifact directly to final MP4
        dlRes.body?.byteStream()?.use { inputStream ->
            ZipInputStream(inputStream).use { zipStream ->
                var entry = zipStream.nextEntry
                while (entry != null) {
                    if (entry.name.endsWith(".mp4") || entry.name == "final_output.mp4") {
                        FileOutputStream(finalMp4).use { output ->
                            zipStream.copyTo(output)
                        }
                        break
                    }
                    entry = zipStream.nextEntry
                }
            }
        }

        withContext(Dispatchers.Main) {
            onComplete(finalMp4)
        }

    } catch (e: Exception) {
        withContext(Dispatchers.Main) {
            onError(e.localizedMessage ?: "Unknown error occurred")
        }
    }
}
