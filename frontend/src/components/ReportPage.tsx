import React, { useEffect, useState } from 'react';

type SessionInfo = {
  authenticated: boolean;
  username?: string;
  email?: string;
};

type ReportResponse = {
  cdn_url?: string;
  object_key?: string;
  cached?: boolean;
  report?: {
    user_id: string;
    client_name: string;
    report_date: string;
    processed_until: string;
    events_count: number;
    avg_battery_level: number;
    avg_signal_quality: number;
    max_temperature: number;
    total_active_minutes: number;
  };
  error?: string;
};

const authUrl = process.env.REACT_APP_AUTH_URL || 'http://localhost:8000';

const ReportPage: React.FC = () => {
  const [session, setSession] = useState<SessionInfo | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<ReportResponse['report'] | null>(null);
  const [reportUrl, setReportUrl] = useState<string | null>(null);
  const [cached, setCached] = useState<boolean | null>(null);

  useEffect(() => {
    const loadSession = async () => {
      try {
        const response = await fetch(`${authUrl}/auth/session`, {
          credentials: 'include',
        });
        if (!response.ok) {
          setSession({ authenticated: false });
          return;
        }
        setSession(await response.json());
      } catch {
        setSession({ authenticated: false });
      }
    };

    loadSession();
  }, []);

  const login = () => {
    window.location.href = `${authUrl}/auth/login`;
  };

  const logout = async () => {
    await fetch(`${authUrl}/auth/logout`, {
      method: 'POST',
      credentials: 'include',
    });
    setSession({ authenticated: false });
    setReport(null);
    setReportUrl(null);
    setCached(null);
  };

  const downloadReport = async () => {
    if (!session?.authenticated) {
      setError('Not authenticated');
      return;
    }

    try {
      setLoading(true);
      setError(null);

      const response = await fetch(`${authUrl}/api/reports`, {
        credentials: 'include',
      });

      const data: ReportResponse = await response.json();
      if (!response.ok) {
        throw new Error(data.error || 'Failed to load report');
      }
      setReport(data.report || null);
      setReportUrl(data.cdn_url || null);
      setCached(typeof data.cached === 'boolean' ? data.cached : null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'An error occurred');
    } finally {
      setLoading(false);
    }
  };

  if (session === null) {
    return <div>Loading...</div>;
  }

  if (!session.authenticated) {
    return (
      <div className="flex flex-col items-center justify-center min-h-screen bg-gray-100">
        <button
          onClick={login}
          className="px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600"
        >
          Login
        </button>
      </div>
    );
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-screen bg-gray-100">
      <div className="p-8 bg-white rounded-lg shadow-md">
        <h1 className="text-2xl font-bold mb-6">Usage Reports</h1>
        <div className="mb-4 text-sm text-gray-600">
          Signed in as {session.username || session.email || 'user'}
        </div>
        
        <button
          onClick={downloadReport}
          disabled={loading}
          className={`px-4 py-2 bg-blue-500 text-white rounded hover:bg-blue-600 ${
            loading ? 'opacity-50 cursor-not-allowed' : ''
          }`}
        >
          {loading ? 'Generating Report...' : 'Download Report'}
        </button>
        <button
          onClick={logout}
          className="ml-3 px-4 py-2 bg-gray-200 text-gray-800 rounded hover:bg-gray-300"
        >
          Logout
        </button>

        {error && (
          <div className="mt-4 p-4 bg-red-100 text-red-700 rounded">
            {error}
          </div>
        )}

        {reportUrl && (
          <div className="mt-4 p-4 bg-blue-100 text-blue-800 rounded">
            <div>
              Report file: <a className="underline" href={reportUrl} target="_blank" rel="noreferrer">open from CDN</a>
            </div>
            {cached !== null && (
              <div>{cached ? 'Loaded from S3 cache' : 'Generated from OLAP and saved to S3'}</div>
            )}
          </div>
        )}

        {report && (
          <div className="mt-4 p-4 bg-green-100 text-green-800 rounded">
            <div>Owner: {report.client_name}</div>
            <div>Report date: {report.report_date}</div>
            <div>Processed until: {report.processed_until}</div>
            <div>Events: {report.events_count}</div>
            <div>Average battery: {report.avg_battery_level}%</div>
            <div>Average signal: {report.avg_signal_quality}</div>
            <div>Max temperature: {report.max_temperature} C</div>
            <div>Active minutes: {report.total_active_minutes}</div>
          </div>
        )}
      </div>
    </div>
  );
};

export default ReportPage;
