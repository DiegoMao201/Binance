import { NextResponse } from "next/server";
import http from "node:http";
import net from "node:net";

export const dynamic = "force-dynamic";

const CONTAINER_NAMES = [
  "o4w1ns4cceccmn2ozqt7sol2",
  "deriv-bot",
  "l44w84cc4cw8gs8oos0kwkkg",
];

const DOCKER_SOCK = "/var/run/docker.sock";
const DOCKER_TCP_HOSTS = [
  { host: "host.docker.internal", port: 2375 },
  { host: "172.17.0.1", port: 2375 },
  { host: "192.81.216.49", port: 2375 },
];

function dockerRequest(options, body = null) {
  return new Promise((resolve, reject) => {
    const req = http.request(options, (res) => {
      let data = "";
      res.on("data", (c) => (data += c));
      res.on("end", () => resolve({ status: res.statusCode, body: data }));
    });
    req.on("error", reject);
    if (body) req.write(body);
    req.end();
  });
}

async function tryDockerSocket(containerName, action) {
  const path = action === "start"
    ? `/containers/${containerName}/start`
    : `/containers/${containerName}/json`;

  const method = action === "start" ? "POST" : "GET";

  return new Promise((resolve, reject) => {
    const req = http.request(
      {
        socketPath: DOCKER_SOCK,
        path,
        method,
        headers: { "Content-Type": "application/json", "Content-Length": 0 },
      },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode, body: data, via: "socket" }));
      }
    );
    req.on("error", reject);
    req.end();
  });
}

async function tryDockerTCP(host, port, containerName, action) {
  const path = action === "start"
    ? `/containers/${containerName}/start`
    : `/containers/${containerName}/json`;

  const method = action === "start" ? "POST" : "GET";

  return new Promise((resolve, reject) => {
    const req = http.request(
      { host, port, path, method, headers: { "Content-Type": "application/json" } },
      (res) => {
        let data = "";
        res.on("data", (c) => (data += c));
        res.on("end", () => resolve({ status: res.statusCode, body: data, via: `tcp:${host}:${port}` }));
      }
    );
    req.setTimeout(3000, () => { req.destroy(); reject(new Error("timeout")); });
    req.on("error", reject);
    req.end();
  });
}

export async function GET() {
  const results = {};

  // Try socket
  for (const name of CONTAINER_NAMES) {
    try {
      const r = await tryDockerSocket(name, "inspect");
      results[`socket:${name}`] = { status: r.status, body_preview: r.body.slice(0, 200) };
    } catch (e) {
      results[`socket:${name}`] = { error: e.message };
    }
  }

  // Try TCP
  for (const { host, port } of DOCKER_TCP_HOSTS) {
    try {
      const r = await tryDockerTCP(host, port, CONTAINER_NAMES[0], "inspect");
      results[`tcp:${host}:${port}`] = { status: r.status, body_preview: r.body.slice(0, 200) };
    } catch (e) {
      results[`tcp:${host}:${port}`] = { error: e.message };
    }
  }

  return NextResponse.json({ results });
}

export async function POST() {
  const results = {};

  // Try socket start
  for (const name of CONTAINER_NAMES) {
    try {
      const r = await tryDockerSocket(name, "start");
      results[`socket:${name}`] = { status: r.status, body: r.body };
      if (r.status === 204 || r.status === 304) {
        return NextResponse.json({ ok: true, started: name, via: "socket", results });
      }
    } catch (e) {
      results[`socket:${name}`] = { error: e.message };
    }
  }

  // Try TCP start
  for (const { host, port } of DOCKER_TCP_HOSTS) {
    for (const name of CONTAINER_NAMES) {
      try {
        const r = await tryDockerTCP(host, port, name, "start");
        results[`tcp:${host}:${port}:${name}`] = { status: r.status, body: r.body };
        if (r.status === 204 || r.status === 304) {
          return NextResponse.json({ ok: true, started: name, via: `tcp:${host}:${port}`, results });
        }
      } catch (e) {
        results[`tcp:${host}:${port}:${name}`] = { error: e.message };
      }
    }
  }

  return NextResponse.json({ ok: false, message: "No Docker access found", results }, { status: 503 });
}
