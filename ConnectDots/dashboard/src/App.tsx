import { useState, useEffect } from 'react';
import './App.css';

interface Room {
  _id: string;
  metadata: {
    room_name: string;
    building_name: string;
    scanned_at: string;
    device_model: string;
  };
  status: string;
  photo_count: number;
  source?: string;
  cloudinary_scan_url?: string;
  cloudinary_photo_urls?: string[];
  cloudinary_floorplan_url?: string;
  pdf_url?: string;
  audio_url?: string;
  recommendations_pdf_url?: string;
}

interface Conversation {
  conversation_id: string;
  agent_id: string;
  status: string;
  transcript: Array<{
    role: string;
    message: string;
    time_in_call_secs: number;
  }>;
  metadata: {
    start_time_unix_secs: number;
    call_duration_secs: number;
  };
}

function App() {
  const [rooms, setRooms] = useState<Room[]>([]);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [loading, setLoading] = useState(true);
  
  const [hiddenRooms, setHiddenRooms] = useState<Set<string>>(new Set());
  const [hiddenConversations, setHiddenConversations] = useState<Set<string>>(new Set());

  useEffect(() => {
    const fetchData = async () => {
      try {
        const [roomsRes, convsRes] = await Promise.all([
          fetch('http://localhost:8000/rooms'),
          fetch('http://localhost:8000/conversations')
        ]);
        
        const roomsData = await roomsRes.json();
        if (roomsData.rooms) {
          setRooms(roomsData.rooms);
        }
        
        const convsData = await convsRes.json();
        if (convsData.conversations) {
          setConversations(convsData.conversations);
        }
      } catch (error) {
        console.error('Failed to fetch data:', error);
      } finally {
        setLoading(false);
      }
    };

    fetchData();
    const interval = setInterval(fetchData, 10000); // Poll every 10s
    return () => clearInterval(interval);
  }, []);

  if (loading && rooms.length === 0) {
    return <div className="loading">Loading BrailleMap Dashboard...</div>;
  }

  return (
    <div className="app">
      <header className="header">
        <h1>BrailleMap Admin Dashboard</h1>
        <p>Manage your space, monitor accessibility, and review user interactions.</p>
      </header>

      <main className="main-content">
        <section className="section">
          <h2>Room Scans & Layouts</h2>
          <div className="grid">
            {rooms.length === 0 && <p className="empty">No rooms processed yet.</p>}
            {rooms.filter(room => !hiddenRooms.has(room._id)).map(room => (
              <div key={room._id} className="card">
                <div className="card-header">
                  <h3>{room.metadata.room_name} ({room.metadata.building_name})</h3>
                  <button className="btn-delete" onClick={() => setHiddenRooms(prev => new Set(prev).add(room._id))}>&times;</button>
                </div>
                <p className="meta">Status: <span className={`badge ${room.status}`}>{room.status}</span></p>
                <p className="meta">Source: {room.source || 'LiDAR Scan'}</p>
                
                <div className="assets">
                  {room.cloudinary_floorplan_url && (
                    <div className="asset-item">
                      <h4>Floorplan</h4>
                      <img src={room.cloudinary_floorplan_url} alt="Floorplan" className="thumb" />
                      <a href={room.cloudinary_floorplan_url} target="_blank" rel="noreferrer">View File</a>
                    </div>
                  )}
                  {room.cloudinary_photo_urls && room.cloudinary_photo_urls.length > 0 && (
                    <div className="asset-item">
                      <h4>Photos ({room.photo_count})</h4>
                      <div className="photo-grid">
                        {room.cloudinary_photo_urls.slice(0, 3).map((url, i) => (
                          <img key={i} src={url} alt={`Photo ${i}`} className="thumb small" />
                        ))}
                      </div>
                    </div>
                  )}
                  {room.cloudinary_scan_url && (
                    <div className="asset-item link-only">
                      <a href={room.cloudinary_scan_url} target="_blank" rel="noreferrer">📄 Raw Scan JSON</a>
                    </div>
                  )}
                </div>

                <div className="results">
                  <h4>Generated Assets</h4>
                  {room.pdf_url ? (
                    <a href={room.pdf_url} target="_blank" rel="noreferrer" className="btn">Braille PDF</a>
                  ) : <span className="pending">Map Pending...</span>}
                  
                  {room.recommendations_pdf_url ? (
                    <a href={room.recommendations_pdf_url} target="_blank" rel="noreferrer" className="btn">ADA Report</a>
                  ) : <span className="pending">Report Pending...</span>}
                  
                  {room.audio_url ? (
                    <audio controls src={room.audio_url} className="audio-player"></audio>
                  ) : <span className="pending">Audio Pending...</span>}
                </div>
              </div>
            ))}
          </div>
        </section>

        <section className="section">
          <h2>Voice Agent Conversations</h2>
          <p className="subtitle">Review questions asked by blind users to understand their needs.</p>
          <div className="conversation-list">
            {conversations.length === 0 && <p className="empty">No conversations found.</p>}
            {conversations.filter(conv => !hiddenConversations.has(conv.conversation_id)).map(conv => (
              <div key={conv.conversation_id} className="card chat-card">
                <div className="chat-header">
                  <div>
                    <span className="chat-id">ID: {conv.conversation_id.slice(0,8)}</span>
                    <span className="chat-time">Duration: {conv.metadata?.call_duration_secs || 0}s</span>
                  </div>
                  <button className="btn-delete" onClick={() => setHiddenConversations(prev => new Set(prev).add(conv.conversation_id))}>&times;</button>
                </div>
                <div className="transcript">
                  {(conv.transcript || []).map((msg, i) => (
                    <div key={i} className={`message ${msg.role}`}>
                      <strong>{msg.role === 'agent' ? 'Assistant' : 'User'}:</strong> {msg.message}
                    </div>
                  ))}
                  {(!conv.transcript || conv.transcript.length === 0) && (
                    <p className="no-msgs">No transcript available.</p>
                  )}
                </div>
              </div>
            ))}
          </div>
        </section>
      </main>
    </div>
  );
}

export default App;
