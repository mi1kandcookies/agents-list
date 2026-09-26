// Errors carry an HTTP status and a stable machine-readable code.
export class SidecarError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export const badRequest = (message, code = 'INVALID_REQUEST') => new SidecarError(400, code, message);
export const notFound = (message) => new SidecarError(404, 'NOT_FOUND', message);
