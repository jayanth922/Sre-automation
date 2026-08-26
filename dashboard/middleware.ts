import { NextResponse } from 'next/server'
import type { NextRequest } from 'next/server'

export function middleware(request: NextRequest) {
    // Presence of the httpOnly refresh cookie = has a session (the access token
    // lives in memory client-side, so it can't be checked here). The server
    // still validates every request; this only drives redirects.
    const token = request.cookies.get('sentinel_refresh')?.value
    const { pathname } = request.nextUrl

    // Define public paths that don't depend on auth
    const isPublicPath = pathname === '/login' || pathname === '/register' || pathname.startsWith('/_next') || pathname.startsWith('/api') || pathname.startsWith('/static') || pathname.startsWith('/auth')

    if (!isPublicPath && !token) {
        // Redirect to login if accessing protected route without token
        const loginUrl = new URL('/login', request.url)
        return NextResponse.redirect(loginUrl)
    }

    if (isPublicPath && token && (pathname === '/login' || pathname === '/register')) {
        // Redirect to dashboard if already logged in and trying to access auth pages
        const dashboardUrl = new URL('/', request.url)
        return NextResponse.redirect(dashboardUrl)
    }

    return NextResponse.next()
}

export const config = {
    matcher: [
        /*
         * Match all request paths except for the ones starting with:
         * - api (API routes)
         * - _next/static (static files)
         * - _next/image (image optimization files)
         * - favicon.ico (favicon file)
         */
        '/((?!api|_next/static|_next/image|favicon.ico).*)',
    ],
}
