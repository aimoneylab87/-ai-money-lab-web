const VIDEO_API_BASE =
    "https://func-ai-money-lab-billing.azurewebsites.net/api";

function setVideoStatus(message, type = "info") {
    const status = document.getElementById("videoGeneratorStatus");

    if (!status) return;

    status.textContent = message;
    status.dataset.type = type;
}

function escapeVideoHtml(value) {
    return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;")
        .replace(/"/g, "&quot;")
        .replace(/'/g, "&#039;");
}

function getCurrentCustomerId() {
    return window.currentCustomer?.id || "";
}

function renderVideoJob(video) {
    const container = document.getElementById("videoPreviewContainer");

    if (!container || !video) return;

    if (video.status === "completed" && video.video_url) {
        container.innerHTML = `
            <div class="video-result">
                <h3>Generated Video</h3>
                <p>${escapeVideoHtml(video.prompt)}</p>

                <video
                    controls
                    playsinline
                    preload="metadata"
                    style="width:100%;max-width:720px;border-radius:12px;"
                    src="${escapeVideoHtml(video.video_url)}">
                </video>

                <div style="margin-top:12px;">
                    <a
                        href="${escapeVideoHtml(video.video_url)}"
                        target="_blank"
                        rel="noopener noreferrer">
                        Open Video
                    </a>
                    &nbsp;|&nbsp;
                    <a
                        href="${escapeVideoHtml(video.video_url)}"
                        download>
                        Download
                    </a>
                </div>
            </div>
        `;
        return;
    }

    if (video.status === "failed") {
        container.innerHTML = `
            <div class="video-result">
                <strong>Video generation failed.</strong>
                <p>${escapeVideoHtml(
                    video.error_message || "Unknown error"
                )}</p>
            </div>
        `;
        return;
    }

    container.innerHTML = `
        <div class="video-result">
            <strong>
                Video status: ${escapeVideoHtml(video.status || "unknown")}
            </strong>
            <p>Your video is being generated.</p>
        </div>
    `;
}

async function getVideoJob(jobId) {
    const customerId = getCurrentCustomerId();

    if (!customerId) {
        throw new Error("Customer account is not available.");
    }

    const response = await apiFetch(
        `${VIDEO_API_BASE}/videos/${encodeURIComponent(jobId)}?customer_id=${encodeURIComponent(customerId)}`
    );

    const data = await response.json();

    if (!response.ok || !data.success) {
        throw new Error(data.error || "Unable to retrieve video job.");
    }

    return data.video;
}

async function pollVideoJob(jobId) {
    const maxAttempts = 120;

    for (let attempt = 0; attempt < maxAttempts; attempt++) {
        const video = await getVideoJob(jobId);

        renderVideoJob(video);

        if (video.status === "completed") {
            setVideoStatus("Video generation completed.", "success");
            return video;
        }

        if (video.status === "failed") {
            setVideoStatus(
                video.error_message || "Video generation failed.",
                "error"
            );
            return video;
        }

        setVideoStatus(
            `Generating video... ${video.status}`,
            "info"
        );

        await new Promise(resolve => setTimeout(resolve, 5000));
    }

    throw new Error(
        "Video generation timed out while waiting for completion."
    );
}

async function generateVideo(event) {
    event.preventDefault();

    const customerId = getCurrentCustomerId();
    const prompt =
        document.getElementById("videoPrompt")?.value.trim();

    const duration = Number(
        document.getElementById("videoDuration")?.value || 8
    );

    const resolution =
        document.getElementById("videoResolution")?.value || "vertical";

    const button =
        document.getElementById("generateVideoButton");

    if (!customerId) {
        setVideoStatus(
            "Please sign in before generating a video.",
            "error"
        );
        return;
    }

    if (!prompt) {
        setVideoStatus(
            "Enter a prompt for the video.",
            "error"
        );
        return;
    }

    try {
        if (button) {
            button.disabled = true;
            button.textContent = "Generating...";
        }

        setVideoStatus(
            "Submitting video generation request...",
            "info"
        );

        const response = await apiFetch(`${VIDEO_API_BASE}/videos`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json"
            },
            body: JSON.stringify({
                customer_id: customerId,
                prompt,
                duration_seconds: duration,
                resolution,
                provider: "sora-2"
            })
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(
                data.error ||
                "Unable to create video generation job."
            );
        }

        renderVideoJob(data.job);

        setVideoStatus(
            "Video job created. Generation has started.",
            "info"
        );

        await pollVideoJob(data.job.id);
        await loadVideos();
    } catch (error) {
        console.error(
            "VIDEO_GENERATION_ERROR:",
            error
        );

        setVideoStatus(
            error.message ||
            "Unable to generate video.",
            "error"
        );
    } finally {
        if (button) {
            button.disabled = false;
            button.textContent = "Generate Video";
        }
    }
}

async function loadVideos() {
    const customerId = getCurrentCustomerId();
    const table =
        document.getElementById("videoHistoryTable");

    if (!customerId || !table) return;

    try {
        const response = await apiFetch(
            `${VIDEO_API_BASE}/videos?customer_id=${encodeURIComponent(customerId)}`
        );

        const data = await response.json();

        if (!response.ok || !data.success) {
            throw new Error(
                data.error ||
                "Unable to load video history."
            );
        }

        const videos = data.videos || [];

        if (!videos.length) {
            table.innerHTML = `
                <tr>
                    <td colspan="6">
                        No videos generated yet.
                    </td>
                </tr>
            `;
            return;
        }

        table.innerHTML = videos.map(video => `
            <tr>
                <td>${escapeVideoHtml(video.prompt)}</td>
                <td>${escapeVideoHtml(video.status)}</td>
                <td>${escapeVideoHtml(video.duration_seconds)}s</td>
                <td>${escapeVideoHtml(video.resolution)}</td>
                <td>
                    ${
                        video.created_at
                            ? escapeVideoHtml(
                                new Date(
                                    video.created_at
                                ).toLocaleString()
                            )
                            : ""
                    }
                </td>
                <td>
                    ${
                        video.video_url
                            ? `<a
                                href="${escapeVideoHtml(video.video_url)}"
                                target="_blank"
                                rel="noopener noreferrer">
                                View
                               </a>`
                            : ""
                    }
                </td>
            </tr>
        `).join("");
    } catch (error) {
        console.error(
            "VIDEO_HISTORY_ERROR:",
            error
        );

        table.innerHTML = `
            <tr>
                <td colspan="6">
                    Unable to load video history.
                </td>
            </tr>
        `;
    }
}

document.addEventListener("DOMContentLoaded", () => {
    const form =
        document.getElementById("videoGeneratorForm");

    if (form) {
        form.addEventListener(
            "submit",
            generateVideo
        );
    }
});
