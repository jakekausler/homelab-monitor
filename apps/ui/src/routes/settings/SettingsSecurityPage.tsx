import { useState, useEffect, useRef } from 'react'
import type { JSX } from 'react'

import { useDeletePin, usePinStatus, useSetPin } from '@/api/security-pin'
import { ApiError } from '@/api/client'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { toast } from 'sonner'

export function SettingsSecurityPage(): JSX.Element {
  const pinStatus = usePinStatus()
  const setPin = useSetPin()
  const deletePin = useDeletePin()

  // Set PIN dialog
  const [setDialogOpen, setSetDialogOpen] = useState(false)
  const [newPin, setNewPin] = useState('')
  const [confirmNewPin, setConfirmNewPin] = useState('')
  const [currentPassword, setCurrentPassword] = useState('')

  // Rotate PIN dialog
  const [rotateDialogOpen, setRotateDialogOpen] = useState(false)
  const [currentPin, setCurrentPin] = useState('')
  const [rotateNewPin, setRotateNewPin] = useState('')
  const [rotateConfirmNewPin, setRotateConfirmNewPin] = useState('')

  // Delete PIN dialog
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false)
  const [deletePassword, setDeletePassword] = useState('')

  // Error messages
  const [setPinError, setSetPinError] = useState<string>('')
  const [rotateError, setRotateError] = useState<string>('')
  const [deleteError, setDeleteError] = useState<string>('')

  // Lockout countdown
  const [rotateRetryAfter, setRotateRetryAfter] = useState<number | undefined>(undefined)
  const [rotateCountdown, setRotateCountdown] = useState<number>(0)
  const [rotateShowError, setRotateShowError] = useState<boolean>(true)
  const wasRotateLockedRef = useRef<boolean>(false)

  const isLoading = pinStatus.isLoading
  const isPinSet = pinStatus.data?.set === true

  const handleSetPin = (): void => {
    setSetPinError('')
    if (newPin !== confirmNewPin) {
      setSetPinError('PINs do not match')
      return
    }
    if (!/^[0-9]{4,12}$/.test(newPin)) {
      setSetPinError('PIN must be 4-12 digits')
      return
    }
    if (!currentPassword) {
      setSetPinError('Current password required')
      return
    }

    setPin.mutate(
      { new_pin: newPin, current_password: currentPassword },
      {
        onSuccess: () => {
          setSetDialogOpen(false)
          setNewPin('')
          setConfirmNewPin('')
          setCurrentPassword('')
          toast.success('PIN set successfully')
        },
        onError: (error) => {
          if (error instanceof ApiError) {
            setSetPinError(error.message || 'Failed to set PIN')
          } else {
            setSetPinError('An error occurred')
          }
        },
      },
    )
  }

  const handleRotatePin = (): void => {
    setRotateError('')
    setRotateShowError(true)
    setRotateRetryAfter(undefined)
    if (rotateNewPin !== rotateConfirmNewPin) {
      setRotateError('New PINs do not match')
      return
    }
    if (!/^[0-9]{4,12}$/.test(rotateNewPin)) {
      setRotateError('PIN must be 4-12 digits')
      return
    }
    if (!currentPin) {
      setRotateError('Current PIN required')
      return
    }

    setPin.mutate(
      { new_pin: rotateNewPin, current_pin: currentPin },
      {
        onSuccess: () => {
          setRotateDialogOpen(false)
          setCurrentPin('')
          setRotateNewPin('')
          setRotateConfirmNewPin('')
          toast.success('PIN rotated successfully')
        },
        onError: (error) => {
          if (error instanceof ApiError) {
            setRotateShowError(true)
            setRotateError(error.message || 'Failed to rotate PIN')
            if (error.retryAfterSeconds !== null) {
              setRotateRetryAfter(error.retryAfterSeconds)
            }
          } else {
            setRotateError('An error occurred')
          }
        },
      },
    )
  }

  const handleDeletePin = (): void => {
    setDeleteError('')
    if (!deletePassword) {
      setDeleteError('Password required')
      return
    }

    deletePin.mutate(
      { current_password: deletePassword },
      {
        onSuccess: () => {
          setDeleteDialogOpen(false)
          setDeletePassword('')
          toast.success('PIN removed')
        },
        onError: (error) => {
          if (error instanceof ApiError) {
            setDeleteError(error.message || 'Failed to delete PIN')
          } else {
            setDeleteError('An error occurred')
          }
        },
      },
    )
  }

  const resetSetDialog = () => {
    setNewPin('')
    setConfirmNewPin('')
    setCurrentPassword('')
    setSetPinError('')
  }

  const resetRotateDialog = () => {
    setCurrentPin('')
    setRotateNewPin('')
    setRotateConfirmNewPin('')
    setRotateError('')
    setRotateShowError(true)
    wasRotateLockedRef.current = false
    setRotateRetryAfter(undefined)
    setRotateCountdown(0)
  }

  const resetDeleteDialog = () => {
    setDeletePassword('')
    setDeleteError('')
  }

  // Countdown effect for rotate PIN lockout
  useEffect(() => {
    if (!rotateRetryAfter || rotateRetryAfter <= 0) {
      // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
      setRotateCountdown(0)
      if (wasRotateLockedRef.current) {
        // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
        setRotateError('')
        // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
        setRotateShowError(false)
      }
      wasRotateLockedRef.current = false
      return
    }
    wasRotateLockedRef.current = true
    // eslint-disable-next-line react-hooks/set-state-in-effect, @eslint-react/set-state-in-effect
    setRotateCountdown(rotateRetryAfter)
    const t = window.setInterval(() => {
      setRotateCountdown((c) => {
        if (c <= 1) {
          window.clearInterval(t)
          if (wasRotateLockedRef.current) {
            setRotateError('')
            setRotateShowError(false)
          }
          wasRotateLockedRef.current = false
          return 0
        }
        return c - 1
      })
    }, 1000)
    return () => window.clearInterval(t)
  }, [rotateRetryAfter])

  if (isLoading) {
    return (
      <div data-testid="settings-security-page" className="text-sm text-muted-foreground">
        Loading security settings…
      </div>
    )
  }

  if (pinStatus.error !== null) {
    return (
      <div data-testid="settings-security-page" className="text-sm text-destructive">
        Failed to load security settings.
      </div>
    )
  }

  return (
    <div data-testid="settings-security-page" className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Security</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-3">
            <p className="text-sm">
              Status:{' '}
              <span data-testid="pin-status" className="font-medium">
                {isPinSet ? 'PIN is set' : 'No PIN configured'}
              </span>
            </p>
            <div className="flex gap-2">
              {!isPinSet ? (
                <Button
                  data-testid="set-pin-button"
                  onClick={() => {
                    resetSetDialog()
                    setSetDialogOpen(true)
                  }}
                  disabled={setPin.isPending}
                >
                  Set PIN
                </Button>
              ) : (
                <>
                  <Button
                    data-testid="rotate-pin-button"
                    onClick={() => {
                      resetRotateDialog()
                      setRotateDialogOpen(true)
                    }}
                    disabled={setPin.isPending}
                  >
                    Rotate PIN
                  </Button>
                  <Button
                    data-testid="delete-pin-button"
                    variant="destructive"
                    onClick={() => {
                      resetDeleteDialog()
                      setDeleteDialogOpen(true)
                    }}
                    disabled={deletePin.isPending}
                  >
                    Delete PIN
                  </Button>
                </>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Set PIN Dialog */}
      <Dialog
        open={setDialogOpen}
        onOpenChange={(open) => {
          setSetDialogOpen(open)
          if (!open) {
            resetSetDialog()
          }
        }}
      >
        <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Set PIN</DialogTitle>
            <DialogDescription>
              Set a numeric PIN for confirming destructive actions
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="set-new-pin">New PIN (4-12 digits)</Label>
              <Input
                id="set-new-pin"
                type="password"
                inputMode="numeric"
                pattern="[0-9]*"
                autoComplete="off"
                maxLength={12}
                minLength={4}
                value={newPin}
                onChange={(e) => setNewPin(e.target.value.replace(/[^0-9]/g, ''))}
                placeholder="4-12 digits"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="set-confirm-pin">Confirm PIN</Label>
              <Input
                id="set-confirm-pin"
                type="password"
                inputMode="numeric"
                pattern="[0-9]*"
                autoComplete="off"
                maxLength={12}
                minLength={4}
                value={confirmNewPin}
                onChange={(e) => setConfirmNewPin(e.target.value.replace(/[^0-9]/g, ''))}
                placeholder="4-12 digits"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="set-current-password">Current password</Label>
              <Input
                id="set-current-password"
                type="password"
                autoComplete="current-password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.currentTarget.value)}
                placeholder="Your password"
              />
            </div>

            {setPinError && (
              <p role="alert" className="text-sm text-destructive">
                {setPinError}
              </p>
            )}
          </div>

          <DialogFooter className="gap-2 sm:gap-0">
            <Button
              variant="outline"
              onClick={() => {
                setSetDialogOpen(false)
                resetSetDialog()
              }}
            >
              Cancel
            </Button>
            <Button
              onClick={handleSetPin}
              disabled={
                setPin.isPending ||
                !newPin ||
                !confirmNewPin ||
                !currentPassword ||
                !/^[0-9]{4,12}$/.test(newPin)
              }
            >
              {setPin.isPending ? 'Working…' : 'Set PIN'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Rotate PIN Dialog */}
      <Dialog
        open={rotateDialogOpen}
        onOpenChange={(open) => {
          setRotateDialogOpen(open)
          if (!open) {
            resetRotateDialog()
          }
        }}
      >
        <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Rotate PIN</DialogTitle>
            <DialogDescription>Replace your existing PIN with a new one</DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="rotate-current-pin">Current PIN</Label>
              <Input
                id="rotate-current-pin"
                type="password"
                inputMode="numeric"
                pattern="[0-9]*"
                autoComplete="off"
                maxLength={12}
                minLength={4}
                value={currentPin}
                onChange={(e) => setCurrentPin(e.target.value.replace(/[^0-9]/g, ''))}
                placeholder="4-12 digits"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="rotate-new-pin">New PIN (4-12 digits)</Label>
              <Input
                id="rotate-new-pin"
                type="password"
                inputMode="numeric"
                pattern="[0-9]*"
                autoComplete="off"
                maxLength={12}
                minLength={4}
                value={rotateNewPin}
                onChange={(e) => setRotateNewPin(e.target.value.replace(/[^0-9]/g, ''))}
                placeholder="4-12 digits"
              />
            </div>

            <div className="space-y-1.5">
              <Label htmlFor="rotate-confirm-pin">Confirm new PIN</Label>
              <Input
                id="rotate-confirm-pin"
                type="password"
                inputMode="numeric"
                pattern="[0-9]*"
                autoComplete="off"
                maxLength={12}
                minLength={4}
                value={rotateConfirmNewPin}
                onChange={(e) => setRotateConfirmNewPin(e.target.value.replace(/[^0-9]/g, ''))}
                placeholder="4-12 digits"
              />
            </div>

            <div className="space-y-1.5">
              {rotateCountdown > 0 && (
                <p role="alert" className="text-sm text-destructive">
                  Too many wrong attempts. Please wait {rotateCountdown}s before trying again.
                </p>
              )}
              {rotateCountdown === 0 && rotateShowError && rotateError && (
                <p role="alert" className="text-sm text-destructive">
                  {rotateError}
                </p>
              )}
            </div>
          </div>

          <DialogFooter className="gap-2 sm:gap-0">
            <Button
              variant="outline"
              onClick={() => {
                setRotateDialogOpen(false)
                resetRotateDialog()
              }}
            >
              Cancel
            </Button>
            <Button
              onClick={handleRotatePin}
              disabled={
                setPin.isPending ||
                !currentPin ||
                !rotateNewPin ||
                !rotateConfirmNewPin ||
                !/^[0-9]{4,12}$/.test(rotateNewPin) ||
                rotateCountdown > 0
              }
            >
              {setPin.isPending ? 'Working…' : 'Rotate PIN'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Delete PIN Dialog */}
      <Dialog
        open={deleteDialogOpen}
        onOpenChange={(open) => {
          setDeleteDialogOpen(open)
          if (!open) {
            resetDeleteDialog()
          }
        }}
      >
        <DialogContent className="max-h-[90vh] w-[95vw] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>Delete PIN</DialogTitle>
            <DialogDescription>
              Removing your PIN will require using the confirmation phrase for destructive actions.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3">
            <div className="space-y-1.5">
              <Label htmlFor="delete-password">Current password</Label>
              <Input
                id="delete-password"
                type="password"
                autoComplete="current-password"
                value={deletePassword}
                onChange={(e) => setDeletePassword(e.currentTarget.value)}
                placeholder="Your password"
              />
            </div>

            {deleteError && (
              <p role="alert" className="text-sm text-destructive">
                {deleteError}
              </p>
            )}
          </div>

          <DialogFooter className="gap-2 sm:gap-0">
            <Button
              variant="outline"
              onClick={() => {
                setDeleteDialogOpen(false)
                resetDeleteDialog()
              }}
            >
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={handleDeletePin}
              disabled={deletePin.isPending || !deletePassword}
            >
              {deletePin.isPending ? 'Working…' : 'Delete PIN'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
